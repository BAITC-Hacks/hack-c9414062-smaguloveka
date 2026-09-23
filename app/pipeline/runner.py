"""Оркестрация обработки совещания: аудио → диаризация → ASR → LLM → протокол. Одна задача за раз (RAM)."""
import json
import queue
import re
import threading
import time
import traceback

import numpy as np

from .. import db
from .audio import load_16k, SR
from .diarize import diarize
from .asr import transcribe
from .text import restore_case, lang_tag, fmt_t
from .llm import extract
from .protocol import build, topic_times

STEPS = ["Подготовка аудио", "Разделение говорящих", "Распознавание речи: RU · KZ · смешанная",
         "Выделение поручений, ответственных и сроков", "Формирование сводки и протокола"]
HUES = [255, 160, 70, 310, 20, 205, 120, 350, 40, 280]

_q: "queue.Queue[int]" = queue.Queue()
_lock = threading.Lock()  # полная обработка — по одной задаче (RAM)
_asr_lock = threading.Lock()  # ASR-модель: обработка и live-распознавание не мешают друг другу надолго


def enqueue(mid: int):
    _q.put(mid)


def _worker():
    while True:
        mid = _q.get()
        try:
            process(mid)
        except Exception as e:  # noqa
            traceback.print_exc()
            db.update("meetings", mid, status="error", error=f"{type(e).__name__}: {e}")


def _live_watchdog():
    """Вкладку с записью закрыли без «Завершить»: если кусков нет 90 с — запись уходит в обработку (пустая — ошибка)."""
    import os
    while True:
        time.sleep(20)
        try:
            for m in db.q("SELECT id, audio_path FROM meetings WHERE status='processing' AND step_label='Идёт запись'"):
                p = m["audio_path"]
                if not p or not os.path.exists(p):
                    continue
                if time.time() - os.path.getmtime(p) < 90:
                    continue
                if os.path.getsize(p) > 20000:
                    live_stop(m["id"])
                else:
                    db.update("meetings", m["id"], status="error", error="Запись прервана: аудио не получено")
        except Exception:
            traceback.print_exc()


def start_worker():
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_live_watchdog, daemon=True).start()
    # незавершённые после рестарта — снова в очередь; прерванные live-записи без аудио — ошибка
    import os
    for m in db.q("SELECT id, audio_path, source, step_label FROM meetings WHERE status IN ('queued','processing')"):
        p = m["audio_path"]
        if m["step_label"] == "Идёт запись":
            continue  # идущая live-запись: ею займётся сторож (_live_watchdog)
        if not p or not os.path.exists(p) or os.path.getsize(p) < 2000:
            db.update("meetings", m["id"], status="error", error="Запись прервана: аудио не получено")
        else:
            enqueue(m["id"])


def _progress(mid, step, pct):
    db.update("meetings", mid, status="processing", step=step, pct=pct, step_label=STEPS[step])


def _run_llm(transcript: str, date: str):
    n_tok = len(transcript) // 3
    num_ctx = int(min(32768, max(8192, n_tok * 2 + 4096)))
    last_err = None
    for variant in ("v1", "v3"):
        data, _, metrics = extract.run(transcript, date, think=False, variant=variant, num_ctx=num_ctx)
        if data.get("_raw") is not None:
            raw = data["_raw"]
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception as e:
                    last_err = e
                    continue
            else:
                last_err = data.get("_error")
                continue
        metrics["variant"] = variant
        return data, metrics
    raise RuntimeError(f"LLM не вернула корректный JSON: {last_err}")


def process(mid: int):
    m = db.one("SELECT * FROM meetings WHERE id=?", (mid,))
    if not m:
        return
    from .. import setup
    missing = [c["title"] for c in setup.status()["components"] if not c["installed"]]
    if missing:
        db.update("meetings", mid, status="error",
                  error="Не установлены модели: " + "; ".join(missing) + ". Откройте «Настройки» → «Мастер настройки».")
        return
    with _lock:
        t0 = time.time()
        _progress(mid, 0, 5)
        audio = load_16k(m["audio_path"])
        dur = len(audio) / SR
        db.update("meetings", mid, duration_s=round(dur, 2))

        _progress(mid, 1, 12)
        hint = [p for p in (m.get("participants") or []) if p]
        n = m.get("num_speakers") or (len(hint) if hint else -1)
        turns = diarize(audio, n if n and n > 0 else -1)

        _progress(mid, 2, 25)
        segs = []
        for i, tr in enumerate(turns):
            with _asr_lock:
                text = transcribe(audio, tr.start, tr.end)
            if not text.strip():
                continue
            text = restore_case(text, hint)
            segs.append({"start": round(tr.start, 2), "end": round(tr.end, 2), "speaker": f"SPEAKER_{tr.speaker:02d}",
                         "text": text, "lang": lang_tag(text)})
            if i % 5 == 0:
                db.update("meetings", mid, pct=25 + int(25 * (i + 1) / max(1, len(turns))))
        db.execute("DELETE FROM segments WHERE meeting_id=?", (mid,))
        for i, s in enumerate(segs):
            db.insert("segments", meeting_id=mid, idx=i, **s)
        # говорящие с долей речи (имена — после LLM)
        tot = sum(s["end"] - s["start"] for s in segs) or 1
        labels = sorted({s["speaker"] for s in segs})
        db.execute("DELETE FROM speakers WHERE meeting_id=?", (mid,))
        for k, lab in enumerate(labels):
            share = sum(s["end"] - s["start"] for s in segs if s["speaker"] == lab) / tot
            db.insert("speakers", meeting_id=mid, label=lab, name=None, hue=HUES[k % len(HUES)], pct=round(100 * share),
                      confidence=0)
        asr_s = time.time() - t0

        _progress(mid, 3, 55)
        transcript = "\n".join(f"[{s['speaker']}] {s['text']}" for s in segs)
        extraction, metrics = _run_llm(transcript, m["date"])
        metrics["asr_diar_s"] = round(asr_s, 1)

        _progress(mid, 4, 92)
        db.update("meetings", mid, extraction=extraction)
        _save_protocol(mid, m, segs, extraction, hint, metrics, t0)
        try:  # подробная сводка — отдельный вызов LLM; её сбой не ломает протокол
            db.update("meetings", mid, status="processing", step=4, pct=96, step_label=STEPS[4])
            from .report import generate
            generate(mid)
        except Exception:
            traceback.print_exc()
        left = db.one("SELECT COUNT(*) n FROM tasks WHERE meeting_id=?", (mid,))["n"]
        db.update("meetings", mid, status="review" if left else "done", step=5, pct=100, step_label="Готово")
    try:
        from .. import reminders
        reminders.run()
    except Exception:
        traceback.print_exc()


def rebuild(mid: int):
    """Пересобрать протокол из сохранённого ответа LLM и сегментов (без ASR и LLM)."""
    m = db.one("SELECT * FROM meetings WHERE id=?", (mid,))
    if not m or not m.get("extraction"):
        raise ValueError("нет сохранённого ответа LLM")
    segs = db.q('SELECT start, "end", speaker, text, lang FROM segments WHERE meeting_id=? ORDER BY idx', (mid,))
    hint = [p for p in (m.get("participants") or []) if p]
    _save_protocol(mid, m, segs, m["extraction"], hint, m.get("llm_metrics") or {}, time.time())


def _save_protocol(mid, m, segs, extraction, hint, metrics, t0):
    transcript = "\n".join(f"[{s['speaker']}] {s['text']}" for s in segs)
    if True:
        speakers, tasks, summary = build(extraction, transcript, m["date"], hint)
        for lab, sp in speakers.items():
            db.execute("UPDATE speakers SET name=?, role=?, confidence=?, name_source=? WHERE meeting_id=? AND label=?",
                       (sp["name"], sp["role"], sp["confidence"], sp["source"], mid, lab))
        summary["topics"] = [{"label": t["label"], "t": fmt_t(t["start"])} for t in topic_times(summary.pop("topics_raw"), segs)]
        db.execute("DELETE FROM tasks WHERE meeting_id=?", (mid,))
        today = m["date"]
        for k, t in enumerate(tasks, 1):
            si = t.pop("seg_idx")
            review = t.pop("review")
            hist = [{"x": f"Создано ИИ из протокола, уверенность {t['conf']}%", "d": today}]
            if review:
                hist.append({"x": "Требует проверки секретарём: " + ", ".join(t["flags"][:2]), "d": today})
            db.insert("tasks", meeting_id=mid, num=f"{mid}-{k}", seg_idx=si,
                      t=fmt_t(segs[si]["start"]) if si is not None and si < len(segs) else "",
                      history=hist, status="work", confirmed=0, **t)
        metrics.setdefault("total_s", round(time.time() - t0, 1))
        status = "review" if tasks else "done"
        db.update("meetings", mid, summary=summary, llm_metrics=metrics, status=status, step=5, pct=100,
                  step_label="Готово", error=None)


# ---------- live: распознавание хвоста записи во время совещания ----------
_live: dict[int, dict] = {}


def live_chunk(mid: int, path: str) -> list[dict]:
    fresh = mid not in _live
    st = _live.setdefault(mid, {"pos": 0.0})
    try:
        audio = load_16k(path)
    except Exception:
        return []
    if fresh and len(audio) > 10 * SR:  # сервер перезапускался посреди записи — продолжаем с текущего места
        st["pos"] = len(audio) / SR - 6
    new = audio[int(st["pos"] * SR):]
    if len(new) < int(1.5 * SR):
        return []
    # режем по самой тихой точке последней секунды, чтобы не резать слово
    tail = new[-SR:]
    win = int(0.1 * SR)
    energy = np.convolve(tail ** 2, np.ones(win) / win, mode="same")
    cut = len(new) - SR + int(np.argmin(energy))
    start = st["pos"]
    with _asr_lock:
        text = transcribe(new, 0, cut / SR, pad=0)
    st["pos"] = start + cut / SR
    if not text.strip():
        return []
    text = restore_case(text)
    return [{"t": fmt_t(start), "x": text, "l": lang_tag(text)}]


def reprocess(mid: int):
    db.update("meetings", mid, status="queued", step=0, pct=0, step_label="В очереди", error=None, report=None)
    enqueue(mid)


def live_stop(mid: int):
    _live.pop(mid, None)
    db.update("meetings", mid, status="queued", step=0, pct=0)
    enqueue(mid)
