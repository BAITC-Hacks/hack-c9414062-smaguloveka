"""Подробная сводка (отчёт по встрече): отдельный вызов LLM по транскрипту с именами говорящих
и уже извлечёнными поручениями. Работает с тем же провайдером, что выбран в настройках."""
import datetime as dt
import json
import re

from .. import db
from .llm import extract
from .protocol import topic_times
from .text import fmt_t

SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "discussion": {"type": "string"},
            "facts": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "tasks": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
        }, "required": ["title", "discussion", "facts", "decisions", "tasks", "risks", "open_questions"]}},
        "participants": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "contribution": {"type": "string"}}, "required": ["name", "contribution"]}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "conclusion": {"type": "string"},
    },
    "required": ["overview", "sections", "participants", "next_steps", "conclusion"],
}

SYSTEM = """Ты — опытный секретарь-аналитик. По транскрипту совещания составь ПОДРОБНЫЙ отчёт для руководителя, который не был на встрече. Совещание может идти на русском, казахском или смешанном языке — отчёт пиши на русском.

Структура:
- overview — 3–5 предложений: цель встречи, кто вёл, главный итог.
- sections — по одному разделу на каждый обсуждавшийся вопрос, в порядке обсуждения. В каждом:
  • title — название вопроса;
  • discussion — подробный пересказ обсуждения 4–8 предложений: кто что доложил, какие позиции и аргументы звучали, к чему пришли;
  • facts — ключевые факты и цифры с пояснением (проценты, суммы, сроки, количества);
  • decisions — принятые решения;
  • tasks — поручения в формате «Ответственный — что сделать — срок»;
  • risks — риски, проблемы и их причины;
  • open_questions — что осталось нерешённым или требует уточнения.
- participants — каждый участник и его вклад/позиция (1–2 предложения).
- next_steps — следующие шаги по порядку.
- conclusion — 2–3 предложения общего вывода.

Опирайся только на транскрипт, ничего не выдумывай. Если в разделе пункта нет — оставь пустой список. Дата совещания: {date}. Ответ — строго JSON по схеме."""


def generate(mid: int) -> dict:
    m = db.one("SELECT * FROM meetings WHERE id=?", (mid,))
    segs = db.q('SELECT idx, start, "end", speaker, text FROM segments WHERE meeting_id=? ORDER BY idx', (mid,))
    if not m or not segs:
        raise ValueError("нет транскрипта")
    names = {sp["label"]: sp["name"] or f"Говорящий {int(sp['label'].split('_')[-1]) + 1}"
             for sp in db.q("SELECT label, name FROM speakers WHERE meeting_id=?", (mid,))}
    lines = [f"[{fmt_t(s['start'])}] {names.get(s['speaker'], s['speaker'])}: {s['text']}" for s in segs]
    tasks = db.q("SELECT title, owner, due, deadline_raw FROM tasks WHERE meeting_id=? ORDER BY id", (mid,))
    known = "\n".join(f"- {t['owner']} — {t['title']} — {t['due'] or t['deadline_raw'] or 'срок не указан'}" for t in tasks)
    user = "Транскрипт совещания:\n\n" + "\n".join(lines)
    if known:
        user += "\n\nУже выделенные поручения (используй их в разделах tasks):\n" + known
    msgs = [{"role": "system", "content": SYSTEM.format(date=m["date"])}, {"role": "user", "content": user}]
    n_tok = len(user) // 3
    content, _, metrics, _ = extract.call(msgs, SCHEMA, think=False, num_ctx=int(min(32768, max(8192, n_tok * 2 + 4096))))
    try:
        rep = json.loads(content)
    except Exception:
        mm = re.search(r"\{.*\}", content or "", re.S)
        if not mm:
            raise RuntimeError("LLM не вернула корректный отчёт")
        rep = json.loads(mm.group(0))
    seg_dicts = [{"start": s["start"], "text": s["text"]} for s in segs]
    times = topic_times([sec.get("title", "") for sec in rep.get("sections", [])], seg_dicts)
    for sec, tt in zip(rep.get("sections", []), times):
        sec["t"] = fmt_t(tt["start"])
    rep["generated"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rep["model"] = metrics.get("model")
    db.update("meetings", mid, report=rep)
    return rep
