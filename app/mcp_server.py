"""MCP-сервер AI Hatshy: даёт ИИ-ассистентам (Claude Code, Claude Desktop и др.) доступ
к совещаниям, стенограммам и поручениям локального AI Hatshy.

Работает как тонкая прослойка над HTTP API уже запущенного веб-сервера
(`uvicorn app.main:app --port 8000`). Транспорт — stdio. Наружу ничего не ходит:
по умолчанию разрешены только адреса localhost (закрытый контур).

Запуск: `.venv/bin/python -m app.mcp_server` из корня проекта (или `scripts/mcp.sh`).
Переменные окружения:
  HATSHY_URL           — адрес API (по умолчанию http://localhost:8000)
  HATSHY_ALLOW_REMOTE  — "1", чтобы разрешить нелокальный адрес (по умолчанию запрещено)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from pydantic import Field

try:  # mcp 2.x: FastMCP переименован в MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover - очень старые версии SDK
    ToolAnnotations = None

BASE_URL = os.getenv("HATSHY_URL", "http://localhost:8000").rstrip("/")
ALLOW_REMOTE = os.getenv("HATSHY_ALLOW_REMOTE", "") == "1"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
TRANSCRIPT_BUDGET = 24000  # символов стенограммы за один вызов get_meeting

START_HINT = "запустите `.venv/bin/python -m uvicorn app.main:app --port 8000` в корне проекта"

STATUS_RU = {"queued": "в очереди", "processing": "обрабатывается", "review": "готово, есть неподтверждённые поручения",
             "done": "готово", "error": "ошибка"}
PRIORITY_RU = {"high": "высокий", "mid": "средний", "low": "низкий"}
LANG_RU = {"RU": "рус", "KZ": "каз", "RU+KZ": "рус+каз"}

logging.getLogger("httpx").setLevel(logging.WARNING)  # не шуметь в stderr на каждый запрос

mcp = FastMCP(
    "ai-hatshy",
    instructions=(
        "AI Hatshy — локальный ИИ-секретарь совещаний (RU/KZ). Инструменты дают доступ к совещаниям, "
        "сводкам, стенограммам и поручениям (ответственный, срок, статус). Даты — в формате YYYY-MM-DD; "
        "«сегодня» берётся с сервера AI Hatshy. Поручение просрочено, если оно в работе и срок раньше сегодняшней даты. "
        "Изменяющие действия (update_task, remind_task, upload_meeting) выполняйте только по явной просьбе пользователя."
    ),
)


# ---------------------------------------------------------------- HTTP

def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


async def _request(method: str, path: str, *, what: str = "", timeout: float = 30.0, **kw) -> httpx.Response:
    """Запрос к API AI Hatshy с понятными русскими ошибками."""
    if not ALLOW_REMOTE and not _is_local(BASE_URL):
        raise ToolError(f"Адрес {BASE_URL} не локальный. MCP-сервер работает только в закрытом контуре "
                        f"(localhost); для другого адреса задайте HATSHY_ALLOW_REMOTE=1.")
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, trust_env=False) as c:
            r = await c.request(method, path, **kw)
    except httpx.ConnectError:
        raise ToolError(f"Сервер AI Hatshy не запущен или недоступен по адресу {BASE_URL}: {START_HINT}.")
    except httpx.TimeoutException:
        raise ToolError(f"Сервер AI Hatshy не ответил за {int(timeout)} с ({method} {path}). Попробуйте ещё раз позже.")
    except httpx.HTTPError as e:
        raise ToolError(f"Ошибка связи с сервером AI Hatshy ({BASE_URL}): {e}")
    if r.status_code >= 400:
        detail = ""
        try:
            d = r.json().get("detail")
            detail = d if isinstance(d, str) else ("; ".join(str(x.get("msg", x)) for x in d) if isinstance(d, list) else "")
        except Exception:
            detail = r.text[:300]
        if detail in ("Not Found", "Method Not Allowed"):
            detail = ""
        if r.status_code == 404:
            raise ToolError(f"{what or 'Объект'} не найдено." if what else f"Не найдено: {detail or path}.")
        raise ToolError(f"Сервер AI Hatshy вернул ошибку {r.status_code}"
                        f"{' (' + what + ')' if what else ''}: {detail or 'без подробностей'}")
    return r


async def _get(path: str, *, what: str = "", **kw) -> Any:
    return (await _request("GET", path, what=what, **kw)).json()


# ---------------------------------------------------------------- форматирование

def _cut(s: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _check_date(value: str, field: str) -> str:
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise ToolError(f"Параметр {field}: ожидается дата в формате YYYY-MM-DD, получено «{value}».")


def _overdue(t: dict, today: str) -> bool:
    return t.get("status") == "work" and bool(t.get("due")) and t["due"] < today


def _task_block(t: dict, today: str, *, with_meeting: bool = False, quote_len: int = 220) -> str:
    due = t.get("due")
    raw = t.get("deadline_raw")
    if due:
        deadline = due + (f" («{_cut(raw, 60)}»)" if raw else "")
    else:
        deadline = f"без точной даты («{_cut(raw, 60)}»)" if raw else "без срока"
    status = "выполнено" if t.get("status") == "done" else "в работе"
    if _overdue(t, today):
        status += ", ПРОСРОЧЕНО"
    owner = t.get("owner") or "не указан"
    if t.get("co_owners"):
        owner += " (соисп.: " + ", ".join(t["co_owners"]) + ")"
    parts = [f"ответственный: {owner}", f"срок: {deadline}", f"статус: {status}",
             f"приоритет: {PRIORITY_RU.get(t.get('priority'), t.get('priority') or '—')}"]
    if t.get("direction"):
        parts.append(f"направление: {t['direction']}")
    parts.append("подтверждено" if t.get("confirmed") else "не подтверждено секретарём")
    lines = [f"- #{t['id']} ({t.get('num', '')}) {_cut(t.get('title'), 240)}", "  " + " · ".join(parts)]
    if with_meeting:
        lines.append(f"  Совещание #{t.get('meeting_id')} «{_cut(t.get('meeting_title'), 80)}» от {t.get('meeting_date')}")
    if t.get("quote"):
        lines.append(f"  Цитата [{t.get('t') or '—'}]: «{_cut(t['quote'], quote_len)}»")
    return "\n".join(lines)


def _meeting_status(m: dict) -> str:
    s = STATUS_RU.get(m.get("status"), m.get("status") or "—")
    if m.get("status") == "processing":
        s += f" {m.get('pct', 0)}% ({m.get('step_label') or ''})"
    if m.get("status") == "error" and m.get("error"):
        s += f": {_cut(m['error'], 150)}"
    return s


def _label_mapper(speakers: list[dict]):
    """SPEAKER_00 → имя говорящего (в сводках LLM иногда остаются технические метки)."""
    names = {sp["label"]: sp["name"] for sp in speakers if sp.get("label") and sp.get("name")}
    if not names:
        return lambda s: s or ""
    rx = re.compile("|".join(re.escape(k) for k in sorted(names, key=len, reverse=True)))
    return lambda s: rx.sub(lambda mm: names[mm.group(0)], s or "")


async def _today() -> str:
    return (await _get("/api/state")).get("today") or dt.date.today().isoformat()


# ---------------------------------------------------------------- инструменты

def _tool(read_only: bool):
    """Регистрирует инструмент; подсказки (annotations) передаются, если их поддерживает версия SDK."""
    params = inspect.signature(mcp.tool).parameters
    kw: dict[str, Any] = {}
    if "annotations" in params and ToolAnnotations is not None:
        kw["annotations"] = ToolAnnotations(readOnlyHint=read_only, destructiveHint=False, openWorldHint=False)
    if "structured_output" in params:
        kw["structured_output"] = False  # отдаём компактный текст, без дублирования в structuredContent
    return mcp.tool(**kw)


@_tool(read_only=True)
async def list_meetings() -> str:
    """Список всех совещаний в AI Hatshy: id, название, дата, длительность, статус обработки, языки,
    число поручений и неподтверждённых поручений. Начинайте с него, чтобы узнать id совещаний."""
    st = await _get("/api/state")
    ms = st.get("meetings", [])
    if not ms:
        return f"Сегодня (по серверу): {st.get('today')}. Совещаний пока нет."
    out = [f"Сегодня (по серверу): {st.get('today')}. Совещаний: {len(ms)}."]
    for m in ms:
        out.append(f"- #{m['id']} «{_cut(m['title'], 90)}» · {m['date']} · {m.get('dur_label') or '—'} · "
                   f"статус: {_meeting_status(m)} · языки: {m.get('langs') or '—'} · "
                   f"поручений: {m.get('tasks_count', 0)} (не подтверждено: {m.get('unconfirmed', 0)})")
    return "\n".join(out)


@_tool(read_only=True)
async def get_meeting(
    meeting_id: Annotated[int, Field(description="id совещания (см. list_meetings)")],
    include_transcript: Annotated[bool, Field(description="Добавить стенограмму «[чч:мм:сс] Имя (язык): текст»")] = False,
    transcript_from: Annotated[int, Field(description="С какой реплики (с 0) выводить стенограмму — для длинных записей")] = 0,
) -> str:
    """Подробности совещания: название, дата, статус, краткое содержание, решения, темы, говорящие (роль, доля речи),
    поручения (ответственный, срок, статус, приоритет, цитата с таймкодом) и подробная сводка, если она сформирована.
    Стенограмма — только при include_transcript=true; длинная выводится порциями (~24 тыс. символов),
    следующую порцию запрашивайте с transcript_from."""
    m = await _get(f"/api/meetings/{meeting_id}", what=f"Совещание #{meeting_id}")
    today = await _today()
    fix = _label_mapper(m.get("speakers", []))
    out = [f"Совещание #{m['id']} «{m['title']}»",
           f"Дата: {m['date']} · длительность: {m.get('dur_label') or '—'} · источник: {m.get('source_label') or '—'} · "
           f"языки: {m.get('langs') or '—'} · статус: {_meeting_status(m)}"]
    if not (include_transcript and transcript_from):  # продолжение стенограммы — без повтора сводки
        s = m.get("summary") or {}
        if s.get("short"):
            out += ["", "Краткое содержание:", _cut(fix(s["short"]), 1500)]
        if s.get("decisions"):
            out += ["", "Принятые решения:"] + [f"- {_cut(fix(d), 300)}" for d in s["decisions"][:20]]
        if s.get("topics"):
            out += ["", "Темы:"] + [f"- [{tp.get('t', '—')}] {_cut(fix(tp.get('label')), 150)}" for tp in s["topics"][:20]]
        if s.get("numbers"):
            out += ["", "Ключевые цифры:"] + [f"- {n.get('value')}: {_cut(fix(n.get('meaning')), 150)}" for n in s["numbers"][:15]]
        if m.get("speakers"):
            out += ["", "Говорящие:"]
            for sp in m["speakers"]:
                role = f" — {_cut(fix(sp['role']), 160)}" if sp.get("role") else ""
                out.append(f"- {sp.get('name') or sp.get('label')}{role} · доля речи {sp.get('pct', 0)}%")
        tasks = m.get("tasks") or []
        out += ["", f"Поручения ({len(tasks)}, не подтверждено: {m.get('unconfirmed', 0)}):"]
        out += [_task_block(t, today, quote_len=300) for t in tasks] or ["- нет"]
        rep = m.get("report")
        if isinstance(rep, dict) and rep:
            out += ["", f"Подробная сводка (сформирована {rep.get('generated', '—')}):"]
            if rep.get("overview"):
                out.append(_cut(fix(rep["overview"]), 1200))
            for sec in rep.get("sections", [])[:12]:
                out.append(f"\n## [{sec.get('t', '—')}] {_cut(fix(sec.get('title')), 160)}")
                if sec.get("discussion"):
                    out.append(_cut(fix(sec["discussion"]), 600))
                for key, label in (("decisions", "Решения"), ("tasks", "Задачи"), ("risks", "Риски"),
                                   ("open_questions", "Открытые вопросы")):
                    items = sec.get(key) or []
                    if items:
                        out.append(f"{label}: " + "; ".join(_cut(fix(x), 200) for x in items[:6]))
            if rep.get("next_steps"):
                out += ["", "Следующие шаги:"] + [f"- {_cut(fix(x), 250)}" for x in rep["next_steps"][:12]]
            if rep.get("conclusion"):
                out += ["", "Итог: " + _cut(fix(rep["conclusion"]), 800)]
    if include_transcript:
        segs = m.get("segments") or []
        start = max(0, min(int(transcript_from or 0), len(segs)))
        out += ["", f"Стенограмма ({len(segs)} реплик" + (f", с реплики {start}" if start else "") + "):"]
        budget, end = TRANSCRIPT_BUDGET, start
        for sg in segs[start:]:
            line = f"[{sg.get('t')}] {sg.get('name') or sg.get('speaker')} ({LANG_RU.get(sg.get('l'), sg.get('l') or '?')}): {sg.get('x', '')}"
            budget -= len(line) + 1
            if budget < 0 and end > start:
                break
            out.append(line)
            end += 1
        if end < len(segs):
            out.append(f"… показаны реплики {start}–{end - 1} из {len(segs)}. Продолжение: "
                       f"get_meeting(meeting_id={meeting_id}, include_transcript=true, transcript_from={end}).")
    return "\n".join(out)


@_tool(read_only=True)
async def list_tasks(
    status: Annotated[str, Field(description="all | work (в работе) | done (выполнено) | overdue (просрочено)")] = "all",
    owner: Annotated[str | None, Field(description="Фильтр по ответственному: часть имени, без учёта регистра")] = None,
    meeting_id: Annotated[int | None, Field(description="Только поручения этого совещания")] = None,
    due_before: Annotated[str | None, Field(description="Срок не позже этой даты включительно, YYYY-MM-DD")] = None,
) -> str:
    """Поручения из всех совещаний с фильтрами по статусу, ответственному, совещанию и сроку.
    Просроченное = в работе и срок раньше сегодняшней даты сервера. Сортировка по сроку (без срока — в конце)."""
    status = (status or "all").strip().lower()
    if status not in ("all", "work", "done", "overdue"):
        raise ToolError("Параметр status: допустимо all, work, done или overdue.")
    before = _check_date(due_before, "due_before") if due_before else None
    st = await _get("/api/state")
    today = st.get("today") or dt.date.today().isoformat()
    tasks = st.get("tasks", [])
    if meeting_id is not None:
        tasks = [t for t in tasks if t.get("meeting_id") == meeting_id]
    if status == "overdue":
        tasks = [t for t in tasks if _overdue(t, today)]
    elif status in ("work", "done"):
        tasks = [t for t in tasks if t.get("status") == status]
    if owner and owner.strip():
        q = owner.strip().casefold()
        tasks = [t for t in tasks if q in (t.get("owner") or "").casefold()
                 or any(q in (c or "").casefold() for c in t.get("co_owners") or [])]
    if before:
        tasks = [t for t in tasks if t.get("due") and t["due"] <= before]
    tasks.sort(key=lambda t: (t.get("due") or "9999-99-99", t["id"]))
    flt = [f"статус={status}"] + ([f"ответственный~«{owner}»"] if owner else []) + \
          ([f"совещание #{meeting_id}"] if meeting_id is not None else []) + ([f"срок ≤ {before}"] if before else [])
    head = f"Сегодня (по серверу): {today}. Фильтр: {', '.join(flt)}. Найдено поручений: {len(tasks)}."
    if not tasks:
        return head
    limit = 60
    body = [_task_block(t, today, with_meeting=meeting_id is None, quote_len=160) for t in tasks[:limit]]
    if len(tasks) > limit:
        body.append(f"… и ещё {len(tasks) - limit}; уточните фильтр.")
    return head + "\n" + "\n".join(body)


@_tool(read_only=False)
async def update_task(
    task_id: Annotated[int, Field(description="id поручения (число после # в списках)")],
    status: Annotated[str | None, Field(description="work — в работе, done — выполнено")] = None,
    confirmed: Annotated[bool | None, Field(description="Подтверждено секретарём")] = None,
    due: Annotated[str | None, Field(description="Новый срок YYYY-MM-DD; пустая строка — снять срок")] = None,
    owner: Annotated[str | None, Field(description="Новый ответственный (ФИО или подразделение)")] = None,
    title: Annotated[str | None, Field(description="Новая формулировка поручения")] = None,
    priority: Annotated[str | None, Field(description="high | mid | low")] = None,
) -> str:
    """Изменить поручение: статус (work/done), подтверждение, срок, ответственного, формулировку, приоритет.
    Передавайте только те поля, которые нужно изменить. Все изменения попадают в историю поручения."""
    body: dict[str, Any] = {}
    if status is not None:
        if status not in ("work", "done"):
            raise ToolError("Параметр status: допустимо work (в работе) или done (выполнено).")
        body["status"] = status
    if confirmed is not None:
        body["confirmed"] = bool(confirmed)
    if due is not None:
        body["due"] = _check_date(due, "due") if due.strip() else None
    if owner is not None:
        if not owner.strip():
            raise ToolError("Параметр owner не может быть пустым.")
        body["owner"] = owner.strip()
    if title is not None:
        if not title.strip():
            raise ToolError("Параметр title не может быть пустым.")
        body["title"] = title.strip()
    if priority is not None:
        if priority not in PRIORITY_RU:
            raise ToolError("Параметр priority: допустимо high, mid или low.")
        body["priority"] = priority
    if not body:
        raise ToolError("Не указано ни одного поля для изменения (status, confirmed, due, owner, title, priority).")
    t = (await _request("PATCH", f"/api/tasks/{task_id}", json=body, what=f"Поручение #{task_id}")).json()
    today = await _today()
    changed = ", ".join(f"{k}={'—' if v is None else v}" for k, v in body.items())
    return f"Поручение #{task_id} обновлено ({changed}).\n" + _task_block(t, today, with_meeting=True)


@_tool(read_only=False)
async def remind_task(task_id: Annotated[int, Field(description="id поручения")]) -> str:
    """Отправить напоминание ответственному по поручению прямо сейчас (уведомление в приложении и письмо
    в локальный outbox — наружу ничего не уходит)."""
    r = (await _request("POST", f"/api/tasks/{task_id}/remind", what=f"Поручение #{task_id}")).json()
    n = r.get("notification") or {}
    return (f"Напоминание по поручению #{task_id} создано.\n"
            f"Заголовок: {n.get('title', '—')}\nТекст: {n.get('text', '—')}\nКанал: {n.get('channel', '—')}")


@_tool(read_only=True)
async def search_transcripts(
    query: Annotated[str, Field(description="Искомая подстрока (без учёта регистра), например «срок» или фамилия")],
    limit: Annotated[int, Field(description="Максимум результатов (1–100)")] = 20,
) -> str:
    """Поиск по стенограммам всех совещаний (без учёта регистра). Возвращает совещание, таймкод, говорящего и реплику."""
    q = (query or "").strip()
    if not q:
        raise ToolError("Пустой запрос: укажите слово или фразу для поиска.")
    limit = max(1, min(int(limit or 20), 100))
    ms = (await _get("/api/state")).get("meetings", [])
    sem = asyncio.Semaphore(4)

    async def load(mid: int):
        async with sem:
            try:
                return await _get(f"/api/meetings/{mid}", what=f"Совещание #{mid}")
            except ToolError:
                return None  # совещание могли удалить между запросами

    details = await asyncio.gather(*(load(m["id"]) for m in ms))
    qf = q.casefold()
    hits, total = [], 0
    for m in details:
        if not m:
            continue
        for sg in m.get("segments") or []:
            text = sg.get("x") or ""
            pos = text.casefold().find(qf)
            if pos < 0:
                continue
            total += 1
            if len(hits) >= limit:
                continue
            start = max(0, pos - 120)
            frag = ("…" if start else "") + text[start:pos + len(q) + 180] + ("…" if pos + len(q) + 180 < len(text) else "")
            hits.append(f"- #{m['id']} «{_cut(m['title'], 70)}» [{sg.get('t')}] {sg.get('name') or sg.get('speaker')}: "
                        f"{_cut(frag, 340)}")
    if not total:
        return f"По запросу «{q}» ничего не найдено (просмотрено совещаний: {len(ms)})."
    head = f"По запросу «{q}» найдено реплик: {total}" + (f", показано {len(hits)}." if total > len(hits) else ".")
    return head + "\n" + "\n".join(hits)


@_tool(read_only=False)
async def upload_meeting(
    file_path: Annotated[str, Field(description="Путь к локальному аудио/видео (mp3, wav, m4a, ogg, webm, mp4 …)")],
    title: Annotated[str, Field(description="Название совещания (по умолчанию — имя файла)")] = "",
    date: Annotated[str, Field(description="Дата совещания YYYY-MM-DD (по умолчанию — сегодня)")] = "",
    participants: Annotated[str, Field(description="Участники через запятую — помогает распознать имена")] = "",
) -> str:
    """Загрузить локальную запись совещания в AI Hatshy и запустить обработку (распознавание, говорящие,
    поручения, сводка). Возвращает id совещания; ход обработки — через get_processing_status."""
    p = Path(file_path).expanduser()
    if not p.is_file():
        raise ToolError(f"Файл не найден: {p}")
    if p.stat().st_size == 0:
        raise ToolError(f"Файл пустой: {p}")
    data = {"title": title.strip(), "participants": participants.strip(), "source": "file"}
    if date.strip():
        data["date"] = _check_date(date, "date")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    with p.open("rb") as f:
        r = (await _request("POST", "/api/meetings", data=data, files={"file": (p.name, f, mime)},
                            timeout=600.0, what="загрузка записи")).json()
    mid = r.get("id")
    size_mb = p.stat().st_size / 1_048_576
    return (f"Запись «{p.name}» ({size_mb:.1f} МБ) загружена, создано совещание #{mid}. Обработка идёт в фоне — "
            f"проверяйте ход через get_processing_status(meeting_id={mid}).")


@_tool(read_only=True)
async def get_processing_status(meeting_id: Annotated[int, Field(description="id совещания")]) -> str:
    """Ход обработки совещания: статус, процент, текущий этап и список этапов пайплайна."""
    p = await _get(f"/api/meetings/{meeting_id}/progress", what=f"Совещание #{meeting_id}")
    st, step, steps = p.get("status"), int(p.get("step") or 0), p.get("steps") or []
    out = [f"Совещание #{meeting_id}: {STATUS_RU.get(st, st)} · {p.get('pct', 0)}% · этап: {p.get('step_label') or '—'}"]
    if p.get("error"):
        out.append(f"Ошибка: {_cut(p['error'], 500)}")
    for i, name in enumerate(steps):
        if st in ("done", "review") or i < step:
            mark = "[x]"
        elif i == step and st == "processing":
            mark = "[>]"
        elif i == step and st == "error":
            mark = "[!]"
        else:
            mark = "[ ]"
        out.append(f"{mark} {i + 1}. {name}")
    if st in ("done", "review"):
        out.append("Обработка завершена — подробности через get_meeting.")
    return "\n".join(out)


@_tool(read_only=False)
async def export_protocol(
    meeting_id: Annotated[int, Field(description="id совещания")],
    fmt: Annotated[str, Field(description="docx или pdf")] = "docx",
    anonymize: Annotated[bool, Field(description="Заменить ФИО на «Участник 1…N»")] = False,
    output_dir: Annotated[str, Field(description="Папка для сохранения файла")] = "~/Downloads",
) -> str:
    """Сформировать протокол совещания (DOCX или PDF: сводка, решения, поручения, стенограмма, участники, подписи)
    и сохранить файл на диск. Возвращает путь к сохранённому файлу."""
    fmt = (fmt or "docx").lower().lstrip(".")
    if fmt not in ("docx", "pdf"):
        raise ToolError("Параметр fmt: допустимо docx или pdf.")
    r = await _request("GET", f"/api/meetings/{meeting_id}/export", params={"fmt": fmt, "anon": int(bool(anonymize))},
                       timeout=180.0, what=f"Совещание #{meeting_id}")
    if not r.content or "json" in r.headers.get("content-type", ""):
        raise ToolError("Сервер не вернул файл протокола.")
    d = Path(output_dir or "~/Downloads").expanduser()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ToolError(f"Не удалось создать папку {d}: {e}")
    stem = f"protocol_{meeting_id}" + ("_anon" if anonymize else "")
    path, i = d / f"{stem}.{fmt}", 1
    while path.exists():
        path, i = d / f"{stem}_{i}.{fmt}", i + 1
    path.write_bytes(r.content)
    return f"Протокол совещания #{meeting_id} ({fmt.upper()}{', обезличен' if anonymize else ''}) сохранён: {path} " \
           f"({len(r.content) / 1024:.0f} КБ)."


# ---------------------------------------------------------------- ресурс и шаблон запроса

@mcp.resource("hatshy://meetings", name="meetings", description="Список совещаний AI Hatshy (текст)",
              mime_type="text/plain")
async def meetings_resource() -> str:
    return await list_meetings()


@mcp.prompt(name="overdue_digest", description="Сводка по просроченным и горящим поручениям для руководителя")
def overdue_digest() -> str:
    return ("Подготовь краткую сводку для руководителя по поручениям AI Hatshy. "
            "1) Вызови list_tasks(status=\"overdue\") — перечисли просроченные поручения: ответственный, срок, совещание. "
            "2) Вызови list_tasks(status=\"work\") и выдели поручения со сроком в ближайшие 3 дня. "
            "3) Сгруппируй по ответственным и предложи, кому отправить напоминание (не отправляй без подтверждения). "
            "Пиши по-русски, коротко, списками.")


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
