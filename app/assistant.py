"""ИИ-помощник: ответы на вопросы о совещаниях.
Вне встречи — контекст из компактного индекса всех совещаний (темы, решения, поручения, участники)
и фрагментов транскриптов, найденных по словам вопроса. Внутри встречи — вся эта встреча.
Ответ — короткий текст и ссылки на совещания и моменты записи (таймкоды)."""
import datetime as dt
import json
import re

from . import db, reminders
from .pipeline.llm import extract
from .pipeline.text import fmt_t

SCHEMA = {"type": "object", "properties": {
    "answer": {"type": "string"},
    "refs": {"type": "array", "items": {"type": "object", "properties": {
        "meeting_id": {"type": "integer"}, "t": {"type": "string"}, "label": {"type": "string"}},
        "required": ["meeting_id", "t", "label"]}}},
    "required": ["answer", "refs"]}

SYSTEM = """Ты — ИИ-помощник системы протоколирования совещаний AI Hatshy. Отвечаешь сотруднику на вопросы о его совещаниях, поручениях и договорённостях.
Правила:
- Опирайся ТОЛЬКО на данные из контекста. Если ответа там нет — честно скажи, что в протоколах этого нет.
- Отвечай НА {lang_name} ЯЗЫКЕ (язык интерфейса пользователя), даже если протоколы на другом языке. Кратко: 1–5 предложений или короткий список. Имена, сроки и цифры — точно как в контексте; label в refs — тоже на этом языке.
- Если вопрос о теме или проекте без названия встречи — найди подходящие совещания по смыслу и назови их.
- В refs укажи совещания (meeting_id из контекста), на которые опирается ответ; t — таймкод момента записи в формате чч:мм:сс, если он известен из фрагмента, иначе пустая строка; label — 2–5 слов, что там.
- В тексте ответа НЕ пиши id совещаний — называй встречу по названию и дате; id указывай только в refs.
- Не выдумывай совещания, людей и сроки.
Сегодня {today}."""

STOP = set("""что как где когда кто кого кому чем про для это эта эти был была были было есть мне меня мой моя
надо нужно ли или над под при все всех всем свой свою себя там тут уже еще ещё очень какие какой какая каких""".split())


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яёәғқңөұүһіa-z0-9]+", (text or "").lower()) if len(w) >= 4 and w not in STOP}


def _names(mid: int) -> dict:
    return {s["label"]: s["name"] or f"Говорящий {int(s['label'].split('_')[-1]) + 1}"
            for s in db.q("SELECT label, name FROM speakers WHERE meeting_id=?", (mid,))}


def _meeting_card(m: dict, full: bool = False) -> str:
    s = m.get("summary") or {}
    tasks = db.q("SELECT title, owner, due, deadline_raw, status FROM tasks WHERE meeting_id=? ORDER BY id", (m["id"],))
    ppl = ", ".join(sorted(set(_names(m["id"]).values())))
    lines = [f"### Совещание id={m['id']}: «{m['title']}», {m['date']}, длительность {int((m['duration_s'] or 0) // 60)} мин",
             f"Участники: {ppl}"]
    if s.get("short"):
        lines.append("Кратко: " + s["short"])
    if s.get("topics"):
        lines.append("Темы: " + "; ".join(f"{t['label']} [{t.get('t', '')}]" for t in s["topics"]))
    if s.get("decisions"):
        lines.append("Решения: " + "; ".join(s["decisions"]))
    if tasks:
        lines.append("Поручения: " + "; ".join(
            f"{t['title']} — {t['owner']} — срок {t['due'] or t['deadline_raw'] or 'не указан'} — "
            f"{'выполнено' if t['status'] == 'done' else 'в работе'}" for t in tasks))
    rep = m.get("report") or {}
    if full and rep:
        if rep.get("overview"):
            lines.append("Обзор: " + rep["overview"])
        for sec in rep.get("sections", []):
            lines.append(f"Раздел «{sec.get('title')}» [{sec.get('t', '')}]: {sec.get('discussion', '')}")
    card = "\n".join(lines)
    return card if full else card[:1500]


def _snippets(question: str, meeting_ids: list[int], limit: int = 12) -> list[str]:
    qs = _stems(question)
    if not qs:
        return []
    scored = []
    for mid in meeting_ids:
        names = _names(mid)
        title = (db.one("SELECT title FROM meetings WHERE id=?", (mid,)) or {}).get("title", "")
        for g in db.q('SELECT start, speaker, text FROM segments WHERE meeting_id=?', (mid,)):
            sc = len(qs & _stems(g["text"]))
            if sc:
                scored.append((sc, f"[совещание id={mid} «{title}», {fmt_t(g['start'])}] {names.get(g['speaker'], g['speaker'])}: {g['text'][:260]}"))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


LANGS = {"ru": "РУССКОМ", "kk": "КАЗАХСКОМ", "en": "АНГЛИЙСКОМ"}


def ask(question: str, meeting_id: int | None = None, history: list | None = None, lang: str = "ru") -> dict:
    question = (question or "").strip()
    if not question:
        raise ValueError("пустой вопрос")
    today = reminders.today().isoformat()
    if meeting_id:
        m = db.one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
        if not m:
            raise ValueError("совещание не найдено")
        names = _names(meeting_id)
        segs = db.q('SELECT start, speaker, text FROM segments WHERE meeting_id=? ORDER BY idx', (meeting_id,))
        tr = "\n".join(f"[{fmt_t(g['start'])}] {names.get(g['speaker'], g['speaker'])}: {g['text']}" for g in segs)
        if len(tr) > 24000:  # длинная встреча: начало + фрагменты, найденные по вопросу
            tr = tr[:12000] + "\n…\nФрагменты по теме вопроса:\n" + "\n".join(_snippets(question, [meeting_id], 25))
        context = "Пользователь сейчас открыл это совещание — вопрос, скорее всего, о нём.\n\n" + \
                  _meeting_card(m, full=True) + "\n\nТранскрипт:\n" + tr
        scope = "meeting"
    else:
        ms = db.q("SELECT * FROM meetings WHERE status IN ('done','review') ORDER BY date DESC, id DESC")
        cards, total = [], 0
        for m in ms:
            c = _meeting_card(m)
            if total + len(c) > 20000:
                break
            cards.append(c)
            total += len(c)
        snips = _snippets(question, [m["id"] for m in ms])
        context = "Совещания (от новых к старым):\n\n" + "\n\n".join(cards)
        if snips:
            context += "\n\nФрагменты транскриптов, похожие на вопрос:\n" + "\n".join(snips)
        scope = "all"
    hist = ""
    for h in (history or [])[-6:]:
        who = "Пользователь" if h.get("role") == "user" else "Помощник"
        hist += f"{who}: {str(h.get('text', ''))[:600]}\n"
    user = f"КОНТЕКСТ:\n{context}\n\n" + (f"ПРЕДЫДУЩИЙ ДИАЛОГ:\n{hist}\n" if hist else "") + f"ВОПРОС: {question}"
    msgs = [{"role": "system", "content": SYSTEM.format(today=today, lang_name=LANGS.get(lang, LANGS["ru"]))},
            {"role": "user", "content": user}]
    content, _, metrics, _ = extract.call(msgs, SCHEMA, think=False, num_ctx=int(min(32768, max(8192, len(user) // 3 + 2048))))
    try:
        data = json.loads(content)
    except Exception:
        mm = re.search(r"\{.*\}", content or "", re.S)
        data = json.loads(mm.group(0)) if mm else {"answer": (content or "").strip(), "refs": []}
    refs, seen = [], set()
    for r in data.get("refs") or []:
        try:
            mid = int(r.get("meeting_id"))
        except Exception:
            continue
        m = db.one("SELECT id, title, date FROM meetings WHERE id=?", (mid,))
        t = r.get("t") or ""
        if not m or (mid, t) in seen or not re.fullmatch(r"(\d{2}:\d{2}:\d{2})?", t):
            continue
        seen.add((mid, t))
        refs.append({"meeting_id": mid, "title": m["title"], "date": m["date"], "t": t, "label": r.get("label") or ""})
    return {"answer": data.get("answer") or "", "refs": refs[:6], "scope": scope, "model": metrics.get("model"),
            "asked": dt.datetime.now().strftime("%H:%M")}
