"""Сценарий 2: напоминания ответственным и куратору о приближении срока и просрочке.
Каналы только локальные: лента уведомлений в приложении и письма в data/outbox (*.eml) — наружу ничего не уходит."""
import datetime as dt
from email.message import EmailMessage

from . import db
from .config import OUTBOX

DEFAULT_RULES = {"d3": True, "d0": True, "daily": True, "esc": True, "extracts": True}
DEFAULT_CHANNELS = {"inapp": True, "email": True, "sed": False}
MONTHS_G = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def today() -> dt.date:
    v = db.kv_get("today")
    return dt.date.fromisoformat(v) if v else dt.date.today()


def human(d: dt.date) -> str:
    return f"{d.day} {MONTHS_G[d.month - 1]}"


def channel_label(ch: dict) -> str:
    parts = ["В приложении"]
    if ch.get("email"):
        parts.append("Email (локальный outbox)")
    return " · ".join(parts)


def write_eml(to: str, subject: str, body: str, key: str) -> str:
    msg = EmailMessage()
    msg["From"] = "AI Hatshy <hatshy@local>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)[:80]
    p = OUTBOX / f"{safe}.eml"
    p.write_bytes(bytes(msg))
    return str(p)


def notify(type_, title, text, key, task_id=None, meeting_id=None, to=None, created=None):
    ch = db.kv_get("channels", DEFAULT_CHANNELS)
    if db.one("SELECT id FROM notifications WHERE key=?", (key,)):
        return None
    created = created or f"{today().isoformat()}T09:00:00"
    nid = db.insert("notifications", type=type_, title=title, text=text, channel=channel_label(ch), created=created,
                    task_id=task_id, meeting_id=meeting_id, read=0, key=key)
    if ch.get("email") and to:
        write_eml(to, title, f"{title}\n{text}\n\n— AI Hatshy (локальный контур)", key)
    return nid


def run(on: dt.date | None = None) -> int:
    d0 = on or today()
    rules = {**DEFAULT_RULES, **(db.kv_get("rules", {}) or {})}
    n = 0
    for t in db.q("SELECT * FROM tasks WHERE status='work' AND due IS NOT NULL AND due<>''"):
        due = dt.date.fromisoformat(t["due"])
        days = (due - d0).days
        who = t["owner"]
        base = f"{who} · {t['title']}"
        cur = (t["issued_by"] or "").strip()
        rcpt = who if not cur or cur == who else f"{who}, {cur} (куратор)"  # исполнитель и куратор поручения
        if rules["d3"] and 0 < days <= 3:
            n += bool(notify("soon", f"Срок через {days} дн.: {t['title']}", f"{who} · до {human(due)}" + (f" · куратор: {cur}" if cur and cur != who else ""),
                             f"soon:{t['id']}:{t['due']}", t["id"], t["meeting_id"], rcpt))
        if rules["d0"] and days == 0:
            n += bool(notify("today", f"Срок сегодня: {t['title']}", f"{who} · до {human(due)}",
                             f"today:{t['id']}:{t['due']}", t["id"], t["meeting_id"], rcpt))
        if rules["daily"] and days < 0:
            n += bool(notify("over", f"Просрочено: {t['title']}", f"{who} · срок был {human(due)}" + (f" · куратор: {cur}" if cur and cur != who else ""),
                             f"over:{t['id']}:{d0.isoformat()}", t["id"], t["meeting_id"], rcpt))
        if rules["esc"] and days <= -2:
            n += bool(notify("over", f"Эскалация куратору: {t['title']}",
                             f"{base} · просрочено на {-days} дн. Куратор: {t['issued_by'] or '—'}",
                             f"esc:{t['id']}:{t['due']}", t["id"], t["meeting_id"], t["issued_by"]))
    return n


def remind_now(task_id: int):
    t = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        return None
    due = f" · до {human(dt.date.fromisoformat(t['due']))}" if t["due"] else ""
    stamp = dt.datetime.now().strftime("%H%M%S")
    nid = notify("soon", f"Напоминание: {t['title']}", f"{t['owner']}{due}", f"manual:{task_id}:{stamp}",
                 task_id, t["meeting_id"], t["owner"], created=f"{today().isoformat()}T{dt.datetime.now():%H:%M:%S}")
    return nid


def send_extracts(meeting_id: int) -> tuple[int, list[str]]:
    m = db.one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
    tasks = db.q("SELECT * FROM tasks WHERE meeting_id=? ORDER BY id", (meeting_id,))
    by = {}
    for t in tasks:
        by.setdefault(t["owner"], []).append(t)
    files = []
    for owner, ts in by.items():
        lines = [f"Выдержка из протокола: {m['title']} ({m['date']})", f"Ответственный: {owner}", ""]
        for t in ts:
            due = t["due"] or t["deadline_raw"] or "не указан"
            lines.append(f"• {t['title']} — срок: {due}")
            if t["quote"]:
                lines.append(f"  Фрагмент стенограммы [{t['t']}]: «{t['quote']}»")
        files.append(write_eml(owner, f"Поручения по итогам совещания «{m['title']}»", "\n".join(lines),
                               f"extract_{meeting_id}_{owner}"))
    notify("info", "Выдержки из протокола разосланы", f"{m['title']} · {len(by)} ответственных получили свои поручения",
           f"extracts:{meeting_id}:{dt.datetime.now():%H%M%S}", None, meeting_id,
           created=f"{today().isoformat()}T{dt.datetime.now():%H:%M:%S}")
    return len(by), files
