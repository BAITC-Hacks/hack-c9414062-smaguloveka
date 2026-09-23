"""SQLite-хранилище (один файл data/hatshy.db). JSON-поля хранятся текстом."""
import json
import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, date TEXT, source TEXT, filename TEXT, audio_path TEXT,
  duration_s REAL DEFAULT 0, status TEXT, step INTEGER DEFAULT 0, pct INTEGER DEFAULT 0, step_label TEXT, error TEXT,
  consent INTEGER DEFAULT 0, participants TEXT DEFAULT '[]', num_speakers INTEGER DEFAULT 0, summary TEXT,
  llm_metrics TEXT, created TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS speakers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER, label TEXT, name TEXT, role TEXT, hue INTEGER,
  pct INTEGER, confidence REAL, name_source TEXT);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER, idx INTEGER, start REAL, "end" REAL, speaker TEXT,
  text TEXT, lang TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER, num TEXT, title TEXT, owner TEXT, owner_kind TEXT,
  co_owners TEXT DEFAULT '[]', issued_by TEXT, due TEXT, deadline_raw TEXT, deadline_kind TEXT, status TEXT DEFAULT 'work',
  priority TEXT, direction TEXT, conf INTEGER, quote TEXT, seg_idx INTEGER, t TEXT, confirmed INTEGER DEFAULT 0,
  history TEXT DEFAULT '[]', flags TEXT DEFAULT '[]', created TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, title TEXT, text TEXT, channel TEXT, created TEXT,
  task_id INTEGER, meeting_id INTEGER, read INTEGER DEFAULT 0, key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

JSON_COLS = {"participants", "summary", "llm_metrics", "co_owners", "history", "flags", "extraction", "report"}


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(meetings)")}
        if "extraction" not in cols:  # сырой ответ LLM — чтобы пересобрать протокол без повторного вызова LLM
            c.execute("ALTER TABLE meetings ADD COLUMN extraction TEXT")
        if "report" not in cols:  # подробная сводка по встрече
            c.execute("ALTER TABLE meetings ADD COLUMN report TEXT")


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def _enc(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def q(sql, args=()):
    with conn() as c:
        return [_row(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def insert(table, **kw):
    cols = ",".join(f'"{k}"' for k in kw)
    with conn() as c:
        cur = c.execute(f"INSERT INTO {table}({cols}) VALUES({','.join('?' * len(kw))})", [_enc(v) for v in kw.values()])
        return cur.lastrowid


def update(table, id_, **kw):
    if not kw:
        return
    sets = ",".join(f'"{k}"=?' for k in kw)
    with conn() as c:
        c.execute(f"UPDATE {table} SET {sets} WHERE id=?", [_enc(v) for v in kw.values()] + [id_])


def execute(sql, args=()):
    with conn() as c:
        c.execute(sql, args)


def kv_get(k, default=None):
    r = one("SELECT v FROM kv WHERE k=?", (k,))
    if not r:
        return default
    try:
        return json.loads(r["v"])
    except Exception:
        return r["v"]


def kv_set(k, v):
    with conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, ensure_ascii=False)))
