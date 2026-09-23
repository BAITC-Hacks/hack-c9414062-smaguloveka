"""AI Hatshy — FastAPI сервер: API по CONTRACT.md + раздача веб/мобильного интерфейса."""
import asyncio
import datetime as dt
import os
import platform
import shutil
import zlib
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, reminders, llm_settings, setup, voice, integrations
from .config import ROOT, UPLOADS, DATA
from .pipeline import runner
from .pipeline.text import fmt_t

app = FastAPI(title="AI Hatshy", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")

MONTHS_S = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
SOURCE = {"file": "Файл", "mic": "Микрофон зала", "conf": "Звук конференции"}
PATRON_END = ("ич", "на", "ны", "улы", "ұлы", "қызы", "кызы")


@app.on_event("startup")
def _startup():
    db.init()
    voice.init()
    integrations.init()
    runner.start_worker()


# ---------- представления ----------
def date_label(iso):
    try:
        d = dt.date.fromisoformat(iso)
        return f"{d.day} {MONTHS_S[d.month - 1]}"
    except Exception:
        return iso or ""


def dur_label(sec):
    if not sec:
        return "—"
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ini(name):
    ws = [w for w in (name or "").replace("«", "").split() if w]
    return ("".join(w[0] for w in ws[:2]) or "?").upper()


def short(name):
    ws = (name or "").split()
    if len(ws) == 2 and ws[1].lower().endswith(PATRON_END):
        return f"{ws[0]} {ws[1][0]}."
    return name if len(name or "") <= 22 else name[:20] + "…"


def hash_hue(name):
    return [255, 160, 70, 310, 20, 205, 120, 350, 40, 280][zlib.crc32((name or "").encode()) % 10]


def speaker_name(sp):
    return sp.get("name") or f"Говорящий {int(sp['label'].split('_')[-1]) + 1}"


def first4(s):
    return (s or "").lower().replace("ё", "е")[:4]


def task_obj(t, m, hue_by_first):
    owner = t["owner"] or "Не определён"
    hue = hue_by_first.get(first4(owner)) or hash_hue(owner)
    return {"id": t["id"], "meeting_id": t["meeting_id"], "meeting_title": m["title"] if m else "",
            "meeting_date": m["date"] if m else "", "num": t["num"], "title": t["title"], "owner": owner,
            "owner_short": short(owner), "ini": ini(owner), "hue": hue, "owner_kind": t["owner_kind"],
            "co_owners": t["co_owners"] or [], "issued_by": t["issued_by"], "due": t["due"] or None,
            "deadline_raw": t["deadline_raw"], "status": t["status"], "priority": t["priority"] or "low",
            "direction": t["direction"] or "Другое", "conf": t["conf"] or 0, "quote": t["quote"] or "", "t": t["t"] or "",
            "seg_idx": t["seg_idx"], "confirmed": bool(t["confirmed"]), "review": (t["conf"] or 0) < 80,
            "history": [{"x": h["x"], "d": date_label(h["d"])} for h in (t["history"] or [])]}


def speakers_of(mid):
    sps = db.q("SELECT * FROM speakers WHERE meeting_id=? ORDER BY label", (mid,))
    for sp in sps:
        sp["name"] = speaker_name(sp)
    return sps


def meeting_obj(m, sps=None, tasks=None, segs_langs=None):
    sps = sps if sps is not None else speakers_of(m["id"])
    tasks = tasks if tasks is not None else db.q("SELECT id, confirmed FROM tasks WHERE meeting_id=?", (m["id"],))
    if segs_langs is None:
        segs_langs = {r["lang"] for r in db.q("SELECT DISTINCT lang FROM segments WHERE meeting_id=?", (m["id"],))}
    langs = " ".join(x for x, k in (("RU", "RU"), ("KZ", "KZ"), ("MIX", "RU+KZ")) if k in segs_langs) or "—"
    ext = Path(m["filename"] or "").suffix.upper().lstrip(".")
    src = SOURCE.get(m["source"], m["source"]) + (f" {ext}" if m["source"] == "file" and ext else "")
    return {"id": m["id"], "title": m["title"], "date": m["date"], "date_label": date_label(m["date"]),
            "duration_s": m["duration_s"] or 0, "dur_label": dur_label(m["duration_s"]), "source": m["source"],
            "source_label": src, "status": m["status"], "step": m["step"] or 0, "pct": m["pct"] or 0,
            "step_label": m["step_label"] or "",
            "people": [{"name": sp["name"], "ini": ini(sp["name"]), "hue": sp["hue"]} for sp in sps],
            "langs": langs, "tasks_count": len(tasks), "unconfirmed": sum(1 for t in tasks if not t["confirmed"]),
            "consent": bool(m["consent"]), "error": m["error"]}


def hue_map(sps):
    return {first4(sp["name"]): sp["hue"] for sp in sps if sp.get("name")}


def notif_obj(n, today):
    c = n["created"] or ""
    time = c[11:16] if c[:10] == today else date_label(c[:10])
    return {"id": n["id"], "type": n["type"], "title": n["title"], "text": n["text"], "channel": n["channel"],
            "time": time, "created": c, "task_id": n["task_id"], "meeting_id": n["meeting_id"], "read": bool(n["read"])}


def settings_obj():
    llm = llm_settings.public()
    used = sum(f.stat().st_size for f in DATA.rglob("*") if f.is_file()) / 1e6
    free = shutil.disk_usage(DATA).free / 1e9
    return {
        "today": reminders.today().isoformat(),
        "rules": {**reminders.DEFAULT_RULES, **(db.kv_get("rules", {}) or {})},
        "channels": {**reminders.DEFAULT_CHANNELS, **(db.kv_get("channels", {}) or {})},
        "privacy": {"anon": False, **(db.kv_get("privacy", {}) or {})},
        "llm": llm,
        "integrations": integrations.list_all(),
        "mcp": {"cmd": f"claude mcp add ai-hatshy -- {ROOT / 'scripts' / 'mcp.sh'}",
                "tools": ["list_meetings", "get_meeting", "list_tasks", "update_task", "remind_task",
                          "search_transcripts", "upload_meeting", "get_processing_status", "export_protocol"]},
        "infra": [{"k": "Сервер приложений", "v": f"{platform.node()} · онлайн"},
                  {"k": "Вычислитель", "v": f"{platform.machine()} · CPU (ONNX) + Metal (Ollama)"},
                  {"k": "Хранилище данных", "v": f"{used:.0f} МБ · свободно {free:.1f} ГБ"},
                  {"k": "Версия", "v": "0.1.0 · 23.09.2026"}],
        "models": [{"k": "Распознавание речи", "d": "RU + KZ + смешанная речь в одной модели, int8 ONNX на CPU",
                    "v": "GigaAM-Multilingual large CTC"},
                   {"k": "Диаризация", "d": "VAD + голосовые эмбеддинги + кластеризация",
                    "v": "NeMo TitaNet-small (sherpa-onnx)"},
                   {"k": "Поручения и саммари",
                    "d": ("Локальная LLM" if llm["provider"] == "ollama" else "Внешний OpenAI-совместимый API (только текст)")
                    + " + проверка цитат и сроков правилами",
                    "v": (llm["ollama_model"] + " (Ollama)") if llm["provider"] == "ollama"
                    else (llm["openai_model"] + " (OpenAI API)")},
                   {"k": "Разбор сроков", "d": "«до пятницы», «жұмаға дейін» → дата", "v": "детерминированный резолвер"}],
    }


# ---------- страницы ----------
@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/m")
def mobile():
    return FileResponse(ROOT / "web" / "mobile.html")


# ---------- API ----------
@app.get("/api/state")
def state():
    today = reminders.today().isoformat()
    ms = db.q("SELECT * FROM meetings ORDER BY date DESC, id DESC")
    all_sps = {m["id"]: speakers_of(m["id"]) for m in ms}
    all_tasks = db.q("SELECT * FROM tasks ORDER BY id")
    mt = {m["id"]: m for m in ms}
    tasks = [task_obj(t, mt.get(t["meeting_id"]), hue_map(all_sps.get(t["meeting_id"], []))) for t in all_tasks]
    meetings = [meeting_obj(m, all_sps[m["id"]], [t for t in all_tasks if t["meeting_id"] == m["id"]]) for m in ms]
    people = {}
    for m in ms:
        langs = {r["lang"] for r in db.q("SELECT DISTINCT lang FROM segments WHERE meeting_id=?", (m["id"],))}
        for sp in all_sps[m["id"]]:
            if not sp.get("name") or sp["name"].startswith("Говорящий"):
                continue
            p = people.setdefault(first4(sp["name"]), {"name": sp["name"], "ini": ini(sp["name"]), "hue": sp["hue"],
                                                        "pos": sp.get("role") or "", "langs": set(), "meetings": 0,
                                                        "open": 0, "voice": False})
            p["meetings"] += 1
            p["langs"] |= {"KZ" if l == "KZ" else "RU" if l == "RU" else "RU · KZ" for l in langs}
            if not p["pos"] and sp.get("role"):
                p["pos"] = sp["role"]
    for t in tasks:
        k = first4(t["owner"])
        if t["status"] != "done":
            if k not in people:
                people[k] = {"name": t["owner"], "ini": t["ini"], "hue": t["hue"],
                             "pos": "Подразделение" if t["owner_kind"] == "department" else "Не участвовал в совещании",
                             "langs": set(), "meetings": 0, "open": 0, "voice": False}
            people[k]["open"] += 1
    vp = {v["key"]: v for v in voice.profiles()}
    for k, p in people.items():
        v = vp.get(voice.key_of(p["name"]))
        p["voice"] = bool(v)
        p["voice_info"] = {"id": v["id"], "seconds": v["seconds"], "source": v["source"]} if v else None
        p["can_sample"] = p["meetings"] > 0
    for p in people.values():
        p["langs"] = " · ".join(sorted({x for l in p["langs"] for x in l.split(" · ")})) or "—"
    notifs = [notif_obj(n, today) for n in db.q("SELECT * FROM notifications ORDER BY created DESC, id DESC LIMIT 200")]
    return {"today": today, "meetings": meetings, "tasks": tasks, "people": list(people.values()),
            "notifications": notifs, "settings": settings_obj()}


def _meeting_or_404(mid):
    m = db.one("SELECT * FROM meetings WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404, "Совещание не найдено")
    return m


@app.get("/api/meetings/{mid}")
def meeting(mid: int):
    m = _meeting_or_404(mid)
    sps = speakers_of(mid)
    by_lab = {sp["label"]: sp for sp in sps}
    segs = db.q("SELECT * FROM segments WHERE meeting_id=? ORDER BY idx", (mid,))
    trows = db.q("SELECT * FROM tasks WHERE meeting_id=? ORDER BY id", (mid,))
    tasks = [task_obj(t, m, hue_map(sps)) for t in trows]
    seg_task = {}
    for t in trows:
        if t["seg_idx"] is not None:
            seg_task.setdefault(t["seg_idx"], t["id"])
    out = meeting_obj(m, sps, trows, {s["lang"] for s in segs})
    s = m["summary"] or {}
    out.update({
        "summary": {"short": s.get("short", ""), "decisions": s.get("decisions", []), "topics": s.get("topics", []),
                    "numbers": s.get("numbers", [])},
        "speakers": [{"id": sp["id"], "label": sp["label"], "name": sp["name"], "role": sp.get("role") or "",
                      "hue": sp["hue"], "ini": ini(sp["name"]), "pct": sp["pct"] or 0,
                      "confidence": sp.get("confidence") or 0} for sp in sps],
        "segments": [{"idx": sg["idx"], "t": fmt_t(sg["start"]), "start": sg["start"], "end": sg["end"],
                      "speaker": sg["speaker"], "name": by_lab.get(sg["speaker"], {}).get("name", sg["speaker"]),
                      "hue": by_lab.get(sg["speaker"], {}).get("hue", 250), "l": sg["lang"], "x": sg["text"],
                      "task_id": seg_task.get(sg["idx"])} for sg in segs],
        "tasks": tasks,
        "report": m.get("report"),
        "llm_metrics": m.get("llm_metrics"),
    })
    return out


@app.get("/api/meetings/{mid}/progress")
def progress(mid: int):
    m = _meeting_or_404(mid)
    return {"status": m["status"], "step": m["step"] or 0, "pct": m["pct"] or 0, "step_label": m["step_label"] or "",
            "steps": runner.STEPS, "error": m["error"]}


def _parse_participants(s):
    return [p.strip() for p in (s or "").replace(";", ",").split(",") if p.strip()]


@app.post("/api/meetings")
async def create_meeting(file: UploadFile = File(...), title: str = Form(""), date: str = Form(""),
                         participants: str = Form(""), num_speakers: int = Form(0), notify: str = Form("1"),
                         source: str = Form("file")):
    date = date or reminders.today().isoformat()
    title = title.strip() or Path(file.filename or "Совещание").stem
    mid = db.insert("meetings", title=title, date=date, source=source, filename=file.filename, status="queued",
                    step=0, pct=0, step_label="В очереди", consent=int(notify in ("1", "true", "on")),
                    participants=_parse_participants(participants), num_speakers=num_speakers or 0)
    dst = UPLOADS / f"{mid}{Path(file.filename or 'a.mp3').suffix or '.bin'}"
    with open(dst, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    db.update("meetings", mid, audio_path=str(dst))
    runner.enqueue(mid)
    return {"id": mid}


@app.delete("/api/meetings/{mid}")
def delete_meeting(mid: int):
    m = _meeting_or_404(mid)
    if m["audio_path"] and os.path.exists(m["audio_path"]):
        os.remove(m["audio_path"])
    for t in ("segments", "speakers", "tasks"):
        db.execute(f"DELETE FROM {t} WHERE meeting_id=?", (mid,))
    db.execute("DELETE FROM notifications WHERE meeting_id=?", (mid,))
    db.execute("DELETE FROM meetings WHERE id=?", (mid,))
    return {"ok": True}


@app.get("/api/meetings/{mid}/audio")
def audio(mid: int):
    m = _meeting_or_404(mid)
    if not m["audio_path"] or not os.path.exists(m["audio_path"]):
        raise HTTPException(404, "Аудио удалено")
    return FileResponse(m["audio_path"])


@app.patch("/api/tasks/{tid}")
async def patch_task(tid: int, request: Request):
    body = await request.json()
    t = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        raise HTTPException(404)
    upd, hist = {}, list(t["history"] or [])
    today = reminders.today().isoformat()
    if "status" in body and body["status"] in ("work", "done") and body["status"] != t["status"]:
        upd["status"] = body["status"]
        hist.append({"x": "Отмечено как выполненное" if body["status"] == "done" else "Возвращено в работу", "d": today})
    if "confirmed" in body:
        upd["confirmed"] = int(bool(body["confirmed"]))
        if body["confirmed"] and not t["confirmed"]:
            hist.append({"x": "Подтверждено секретарём", "d": today})
    for k in ("title", "owner", "due", "priority"):
        if k in body:
            upd[k] = body[k] or None
            hist.append({"x": f"Изменено поле «{k}»", "d": today})
    upd["history"] = hist
    db.update("tasks", tid, **upd)
    _refresh_meeting_status(t["meeting_id"])
    m = db.one("SELECT * FROM meetings WHERE id=?", (t["meeting_id"],))
    return task_obj(db.one("SELECT * FROM tasks WHERE id=?", (tid,)), m, hue_map(speakers_of(t["meeting_id"])))


def _refresh_meeting_status(mid):
    m = db.one("SELECT status FROM meetings WHERE id=?", (mid,))
    if m and m["status"] in ("review", "done"):
        left = db.one("SELECT COUNT(*) n FROM tasks WHERE meeting_id=? AND confirmed=0", (mid,))["n"]
        db.update("meetings", mid, status="review" if left else "done")


@app.post("/api/meetings/{mid}/report")
async def make_report(mid: int):
    _meeting_or_404(mid)
    from .pipeline.report import generate
    try:
        return {"ok": True, "report": await asyncio.to_thread(generate, mid)}
    except Exception as e:
        raise HTTPException(500, f"Не удалось сформировать подробную сводку: {e}")


@app.post("/api/meetings/{mid}/reprocess")
def reprocess(mid: int):
    m = _meeting_or_404(mid)
    if m["status"] in ("queued", "processing"):
        raise HTTPException(409, "Совещание уже обрабатывается")
    if not m["audio_path"] or not os.path.exists(m["audio_path"]):
        raise HTTPException(400, "Аудио удалено — переобработка невозможна")
    runner.reprocess(mid)
    return {"ok": True}


@app.post("/api/meetings/{mid}/rebuild")
def rebuild(mid: int):
    _meeting_or_404(mid)
    try:
        runner.rebuild(mid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/meetings/{mid}/confirm_all")
def confirm_all(mid: int):
    today = reminders.today().isoformat()
    for t in db.q("SELECT * FROM tasks WHERE meeting_id=? AND confirmed=0", (mid,)):
        db.update("tasks", t["id"], confirmed=1,
                  history=(t["history"] or []) + [{"x": "Подтверждено секретарём", "d": today}])
    _refresh_meeting_status(mid)
    return {"ok": True}


@app.post("/api/assistant")
async def assistant_ask(request: Request):
    from . import assistant
    b = await request.json()
    try:
        return await asyncio.to_thread(assistant.ask, b.get("question", ""), b.get("meeting_id"), b.get("history") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"ИИ-помощник недоступен: {e}")


@app.get("/api/integrations")
def integ_list():
    return integrations.list_all()


@app.post("/api/integrations")
async def integ_create(request: Request):
    try:
        return integrations.create(await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/integrations/{iid}")
async def integ_update(iid: int, request: Request):
    try:
        return integrations.update(iid, await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "Интеграция не найдена")


@app.delete("/api/integrations/{iid}")
def integ_delete(iid: int):
    integrations.delete(iid)
    return {"ok": True}


@app.post("/api/integrations/test")
async def integ_test(request: Request):
    body = await request.json()
    try:
        return await asyncio.to_thread(integrations.send_test, body.pop("id", None), body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{tid}/send")
async def task_send(tid: int, request: Request):
    body = await request.json()
    try:
        return await asyncio.to_thread(integrations.send_task, int(body["integration_id"]), tid)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"Не удалось отправить: {e}")


@app.post("/api/tasks/{tid}/remind")
def remind(tid: int):
    nid = reminders.remind_now(tid)
    if not nid:
        raise HTTPException(404)
    n = db.one("SELECT * FROM notifications WHERE id=?", (nid,))
    return {"ok": True, "notification": notif_obj(n, reminders.today().isoformat())}


@app.post("/api/meetings/{mid}/send_extracts")
def send_extracts(mid: int):
    _meeting_or_404(mid)
    n, files = reminders.send_extracts(mid)
    return {"ok": True, "sent": n, "outbox": [os.path.relpath(f, ROOT) for f in files]}


@app.patch("/api/speakers/{sid}")
async def rename_speaker(sid: int, request: Request):
    body = await request.json()
    sp = db.one("SELECT * FROM speakers WHERE id=?", (sid,))
    if not sp:
        raise HTTPException(404)
    new = (body.get("name") or "").strip() or None
    old = sp["name"]
    db.update("speakers", sid, name=new, name_source="вручную", confidence=1.0)
    if old and new:
        db.execute("UPDATE tasks SET owner=? WHERE meeting_id=? AND owner=?", (new, sp["meeting_id"], old))
    return {"ok": True}


@app.get("/api/meetings/{mid}/export")
def export(mid: int, fmt: str = "pdf", sections: str = "summary,tasks,transcript,people,sign", anon: int = 0):
    data = meeting(mid)
    secs = {s for s in sections.split(",") if s}
    if fmt == "docx":
        from .export.docx_export import build_docx
        blob = build_docx(data, secs, bool(anon))
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        from .export.pdf_export import build_pdf
        blob, mime, fmt = build_pdf(data, secs, bool(anon)), "application/pdf", "pdf"
    name = f"protocol_{mid}.{fmt}"
    return Response(blob, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/settings")
async def set_settings(request: Request):
    body = await request.json()
    if body.get("today"):
        dt.date.fromisoformat(body["today"])
        db.kv_set("today", body["today"])
    for k in ("rules", "channels", "privacy"):
        if isinstance(body.get(k), dict):
            db.kv_set(k, {**(db.kv_get(k, {}) or {}), **body[k]})
    return settings_obj()


@app.get("/api/setup")
def setup_status():
    return setup.status()


@app.post("/api/setup/download")
async def setup_download(request: Request):
    body = await request.json()
    return setup.start_download(body.get("id") or "all")


@app.post("/api/setup/ollama_pull")
async def setup_ollama_pull(request: Request):
    body = await request.json()
    model = (body.get("model") or "").strip()
    if not model or "cloud" in model:
        raise HTTPException(400, "Укажите локальную модель Ollama")
    return setup.start_ollama_pull(model)


@app.post("/api/setup/complete")
async def setup_complete(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return setup.complete(body.get("done", True))


@app.get("/api/voices")
def voices_list():
    return [{k: v for k, v in p.items() if k != "embedding"} for p in voice.profiles()]


@app.post("/api/voices/from_meeting")
async def voices_from_meeting(request: Request):
    body = await request.json()
    try:
        return await asyncio.to_thread(voice.from_meeting, body.get("name", ""), body.get("meeting_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/voices/from_range")
async def voices_from_range(request: Request):
    b = await request.json()
    try:
        return await asyncio.to_thread(voice.from_range, b.get("name", ""), int(b["meeting_id"]),
                                       float(b["start"]), float(b["end"]))
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/voices/record")
async def voices_record(name: str = Form(...), file: UploadFile = File(...)):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "a.webm").suffix or ".webm", delete=False) as f:
        f.write(await file.read())
        tmp = f.name
    try:
        return await asyncio.to_thread(voice.from_recording, name, tmp)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        os.remove(tmp)  # исходная запись образца не хранится — только вектор голоса


@app.delete("/api/voices/{vid}")
def voices_delete(vid: int):
    db.execute("DELETE FROM voice_profiles WHERE id=?", (vid,))
    return {"ok": True}


@app.get("/api/llm")
def llm_get():
    return {**llm_settings.public(), "ollama_models": llm_settings.ollama_models()}


@app.post("/api/llm")
async def llm_set(request: Request):
    return llm_settings.save(await request.json())


@app.post("/api/llm/test")
async def llm_test(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await asyncio.to_thread(llm_settings.test, body)


@app.post("/api/reminders/run")
def run_reminders():
    return {"created": reminders.run()}


@app.post("/api/notifications/read_all")
def read_all():
    db.execute("UPDATE notifications SET read=1")
    return {"ok": True}


# ---------- live: запись в браузере, распознавание хвоста каждые ~4 с ----------
@app.post("/api/live/start")
async def live_start(request: Request):
    body = await request.json()
    today = reminders.today().isoformat()
    mid = db.insert("meetings", title=(body.get("title") or f"Совещание {today}").strip(), date=today,
                    source=body.get("source") or "mic", filename="live.webm", status="processing", step=0, pct=0,
                    step_label="Идёт запись", consent=int(bool(body.get("notify", True))),
                    participants=_parse_participants(body.get("participants", "")), num_speakers=0)
    path = UPLOADS / f"{mid}.webm"
    path.write_bytes(b"")
    db.update("meetings", mid, audio_path=str(path))
    return {"id": mid}


@app.post("/api/live/{mid}/chunk")
async def live_chunk(mid: int, chunk: UploadFile = File(...)):
    m = db.one("SELECT * FROM meetings WHERE id=?", (mid,))
    if not m or m["status"] != "processing" or m["step_label"] != "Идёт запись":
        return {"segments": [], "stop": True}  # запись уже завершена/удалена — клиент должен остановить рекордер
    with open(m["audio_path"], "ab") as f:
        f.write(await chunk.read())
    segs = await asyncio.to_thread(runner.live_chunk, mid, m["audio_path"])
    return {"segments": segs}


@app.post("/api/live/{mid}/stop")
def live_stop(mid: int):
    _meeting_or_404(mid)
    runner.live_stop(mid)
    return {"ok": True}
