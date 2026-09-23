"""Пользовательские интеграции (исходящие HTTP-вебхуки): СЭД, таск-трекер, Telegram-бот, Bitrix24 и т. п.
Подключение настраивает пользователь: адрес, метод, авторизация, формат (стандартный JSON или свой шаблон
с подстановками {{title}}, {{owner}}, {{due}} …). Поручение отправляется вручную из карточки или автоматически
после формирования протокола. Каждая отправка пишется в журнал и в историю поручения."""
import datetime as dt
import json
import re
import urllib.error
import urllib.request

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS integrations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, method TEXT DEFAULT 'POST', auth TEXT DEFAULT 'none',
  header_name TEXT, token TEXT, fmt TEXT DEFAULT 'standard', template TEXT, auto INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1, created TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS integration_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, integration_id INTEGER, task_id INTEGER, meeting_id INTEGER, event TEXT,
  status INTEGER, ok INTEGER, response TEXT, created TEXT DEFAULT (datetime('now','localtime')));
"""
PLACEHOLDERS = ["title", "owner", "co_owners", "due", "deadline_raw", "priority", "direction", "status", "num",
                "issued_by", "quote", "timecode", "meeting_title", "meeting_date", "task_id", "meeting_id", "event"]
EXAMPLE_TEMPLATE = '{"chat_id": "123456", "text": "Поручение: {{title}}\\nОтветственный: {{owner}}\\nСрок: {{due}}"}'


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)


def _public(r: dict) -> dict:
    tok = r.get("token") or ""
    last = db.one("SELECT status, ok, created FROM integration_log WHERE integration_id=? ORDER BY id DESC LIMIT 1", (r["id"],))
    return {"id": r["id"], "name": r["name"], "url": r["url"], "method": r["method"], "auth": r["auth"],
            "header_name": r["header_name"] or "", "token_set": bool(tok),
            "token_hint": (tok[:3] + "…" + tok[-3:]) if len(tok) > 8 else ("задан" if tok else ""),
            "fmt": r["fmt"], "template": r["template"] or "", "auto": bool(r["auto"]), "enabled": bool(r["enabled"]),
            "last": last}


def list_all() -> list[dict]:
    return [_public(r) for r in db.q("SELECT * FROM integrations ORDER BY id")]


def _clean(body: dict, cur: dict | None = None) -> dict:
    out = {}
    if "name" in body:
        out["name"] = (body.get("name") or "").strip() or "Интеграция"
    if "url" in body:
        url = (body.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            raise ValueError("адрес должен начинаться с http:// или https://")
        out["url"] = url
    if "method" in body:
        out["method"] = body["method"] if body["method"] in ("POST", "PUT", "PATCH") else "POST"
    if "auth" in body:
        out["auth"] = body["auth"] if body["auth"] in ("none", "bearer", "header") else "none"
    if "header_name" in body:
        out["header_name"] = (body.get("header_name") or "").strip()
    if body.get("token"):
        out["token"] = body["token"].strip()
    if body.get("clear_token"):
        out["token"] = ""
    if "fmt" in body:
        out["fmt"] = "template" if body["fmt"] == "template" else "standard"
    if "template" in body:
        out["template"] = body.get("template") or ""
    if (out.get("fmt") or (cur or {}).get("fmt")) == "template":
        render_template(out.get("template", (cur or {}).get("template") or ""), _sample_vars())  # проверка шаблона
    for k in ("auto", "enabled"):
        if k in body:
            out[k] = int(bool(body[k]))
    return out


def create(body: dict) -> dict:
    data = _clean({"method": "POST", "auth": "none", "fmt": "standard", **body})
    if "url" not in data:
        raise ValueError("укажите адрес (URL) подключения")
    iid = db.insert("integrations", **data)
    return _public(db.one("SELECT * FROM integrations WHERE id=?", (iid,)))


def update(iid: int, body: dict) -> dict:
    cur = db.one("SELECT * FROM integrations WHERE id=?", (iid,))
    if not cur:
        raise KeyError(iid)
    db.update("integrations", iid, **_clean(body, cur))
    return _public(db.one("SELECT * FROM integrations WHERE id=?", (iid,)))


def delete(iid: int):
    db.execute("DELETE FROM integrations WHERE id=?", (iid,))


# ---------- формирование и отправка ----------
def task_vars(t: dict, m: dict | None, event: str = "task") -> dict:
    return {"title": t.get("title") or "", "owner": t.get("owner") or "", "co_owners": ", ".join(t.get("co_owners") or []),
            "due": t.get("due") or "", "deadline_raw": t.get("deadline_raw") or "", "priority": t.get("priority") or "",
            "direction": t.get("direction") or "", "status": t.get("status") or "", "num": t.get("num") or "",
            "issued_by": t.get("issued_by") or "", "quote": t.get("quote") or "", "timecode": t.get("t") or "",
            "meeting_title": (m or {}).get("title") or "", "meeting_date": (m or {}).get("date") or "",
            "task_id": str(t.get("id") or ""), "meeting_id": str((m or {}).get("id") or ""), "event": event}


def _sample_vars() -> dict:
    return {k: f"пример {k}" for k in PLACEHOLDERS}


def render_template(tpl: str, vars_: dict):
    """Подставляет {{ключ}} (значения JSON-экранируются) и проверяет, что получился валидный JSON."""
    def sub(mt):
        key = mt.group(1).strip()
        return json.dumps(str(vars_.get(key, "")), ensure_ascii=False)[1:-1]
    text = re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", sub, tpl or "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"шаблон — невалидный JSON: {e.msg} (строка {e.lineno}, позиция {e.colno})")


def standard_payload(t: dict, m: dict | None, event: str) -> dict:
    return {"event": event, "source": "AI Hatshy", "sent_at": dt.datetime.now().isoformat(timespec="seconds"),
            "task": {"id": t.get("id"), "num": t.get("num"), "title": t.get("title"), "owner": t.get("owner"),
                     "co_owners": t.get("co_owners") or [], "issued_by": t.get("issued_by"), "due": t.get("due"),
                     "deadline_raw": t.get("deadline_raw"), "priority": t.get("priority"), "direction": t.get("direction"),
                     "status": t.get("status"), "quote": t.get("quote"), "timecode": t.get("t")},
            "meeting": {"id": (m or {}).get("id"), "title": (m or {}).get("title"), "date": (m or {}).get("date")}}


def _post(integ: dict, payload) -> tuple[int, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "AI-Hatshy/0.1"}
    if integ.get("auth") == "bearer" and integ.get("token"):
        headers["Authorization"] = f"Bearer {integ['token']}"
    elif integ.get("auth") == "header" and integ.get("header_name") and integ.get("token"):
        headers[integ["header_name"]] = integ["token"]
    req = urllib.request.Request(integ["url"], data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers=headers, method=integ.get("method") or "POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status, r.read(500).decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read(500).decode(errors="ignore")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def send_task(iid: int, task_id: int, event: str = "task") -> dict:
    integ = db.one("SELECT * FROM integrations WHERE id=?", (iid,))
    t = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not integ or not t:
        raise KeyError("интеграция или поручение не найдены")
    m = db.one("SELECT id, title, date FROM meetings WHERE id=?", (t["meeting_id"],))
    payload = render_template(integ["template"], task_vars(t, m, event)) if integ["fmt"] == "template" \
        else standard_payload(t, m, event)
    status, resp = _post(integ, payload)
    ok = 200 <= status < 300
    db.insert("integration_log", integration_id=iid, task_id=task_id, meeting_id=t["meeting_id"], event=event,
              status=status, ok=int(ok), response=resp[:500])
    hist = list(t["history"] or [])
    hist.append({"x": f"Отправлено в «{integ['name']}»: " + (f"HTTP {status}" if status else "нет соединения")
                 + ("" if ok else " — ошибка"), "d": dt.date.today().isoformat()})
    db.update("tasks", task_id, history=hist)
    return {"ok": ok, "status": status, "response": resp[:300], "integration": integ["name"]}


def send_test(iid: int | None = None, body: dict | None = None) -> dict:
    """Проверка подключения тестовым поручением (можно до сохранения — по параметрам из формы)."""
    integ = dict(db.one("SELECT * FROM integrations WHERE id=?", (iid,)) or {}) if iid else {}
    integ.update({k: v for k, v in _clean(body or {}, integ).items()})
    if not integ.get("url"):
        raise ValueError("укажите адрес (URL) подключения")
    sample = {"id": 0, "num": "0-0", "title": "Тестовое поручение от AI Hatshy", "owner": "Иван Иванович",
              "co_owners": [], "issued_by": "Председатель", "due": dt.date.today().isoformat(), "deadline_raw": "сегодня",
              "priority": "mid", "direction": "Другое", "status": "work", "quote": "Проверка интеграции", "t": "00:00:00"}
    meet = {"id": 0, "title": "Проверка подключения", "date": dt.date.today().isoformat()}
    payload = render_template(integ.get("template") or "", task_vars(sample, meet, "test")) \
        if integ.get("fmt") == "template" else standard_payload(sample, meet, "test")
    status, resp = _post(integ, payload)
    return {"ok": 200 <= status < 300, "status": status, "response": resp[:300], "payload": payload}


def auto_send_meeting(meeting_id: int) -> int:
    """После формирования протокола: все поручения совещания — в интеграции с включённой автоотправкой."""
    n = 0
    for integ in db.q("SELECT id FROM integrations WHERE enabled=1 AND auto=1"):
        for t in db.q("SELECT id FROM tasks WHERE meeting_id=?", (meeting_id,)):
            try:
                n += bool(send_task(integ["id"], t["id"], "task_created")["ok"])
            except Exception:
                pass
    return n
