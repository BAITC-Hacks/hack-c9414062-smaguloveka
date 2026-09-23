"""Общая логика экспорта протокола (DOCX/PDF): подготовка данных, форматирование, анонимизация.

Чистые функции: на вход — объект совещания из GET /api/meetings/{id}, на выход — готовая к вёрстке
структура `doc` (см. prepare()). Никакого доступа к БД.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime

ALL_SECTIONS = {"summary", "tasks", "transcript", "people", "sign"}

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
LANG_NAMES = {"RU": "русский", "KZ": "казахский", "MIX": "смешанная речь (RU+KZ)", "RU+KZ": "смешанная речь (RU+KZ)"}
SOURCE_LABELS = {"file": "аудиофайл", "mic": "запись с микрофона", "conf": "звук конференции"}

_DEPT_WORDS = ("департамент", "отдел", "служб", "управлен", "комитет", "дирекц", "бухгалтер",
               "аппарат", "филиал", "министерств", "акимат", "институт", "подрядчик", "юрист",
               "группа", "компани", "совет", "правлени", "все ", "каждый")
_GENERIC_RE = re.compile(r"(?i)^(speaker|спикер|говорящий|участник|unknown|неизвестн\w*)[\s_#-]*\d*$")


# ---------------------------------------------------------------- форматирование
def s(x) -> str:
    """None-safe строка."""
    return "" if x is None else str(x).strip()


def parse_iso(d) -> date | None:
    if not d:
        return None
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def fmt_date_short(d) -> str:
    """ISO → dd.mm.yyyy (или исходная строка, если не распарсилось)."""
    dt = parse_iso(d)
    return dt.strftime("%d.%m.%Y") if dt else s(d)


def fmt_date_long(d) -> str:
    dt = parse_iso(d)
    return f"{dt.day} {MONTHS_GEN[dt.month - 1]} {dt.year} г." if dt else s(d)


def fmt_due(task: dict) -> str:
    """Срок поручения: dd.mm.yyyy, иначе формулировка из речи, иначе «не указан»."""
    if task.get("due"):
        return fmt_date_short(task["due"])
    raw = s(task.get("deadline_raw"))
    return raw if raw else "не указан"


def fmt_duration(sec) -> str:
    try:
        total = int(round(float(sec or 0)))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, sec_ = divmod(rem, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    return f"{m} мин {sec_:02d} с" if m else f"{sec_} с"


def fmt_langs(langs) -> str:
    toks = [t for t in re.split(r"[\s,;/]+", s(langs).upper()) if t]
    out = []
    for t in toks:
        name = LANG_NAMES.get(t, t)
        if name not in out:
            out.append(name)
    return ", ".join(out)


def fmt_tc(t) -> str:
    """Таймкод чч:мм:сс из строки или секунд."""
    if isinstance(t, (int, float)):
        t = int(t)
        return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"
    t = s(t)
    parts = t.split(":")
    if len(parts) == 2:
        return f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return t


def is_department(name: str, kind: str | None = None) -> bool:
    if kind == "department":
        return True
    n = s(name)
    if not n:
        return True
    low = n.lower() + " "
    if any(w in low for w in _DEPT_WORDS):
        return True
    # «Имя Отчество» — все слова с заглавной; иначе, скорее всего, подразделение/фраза
    return any(tok[:1].islower() for tok in n.split())


# ---------------------------------------------------------------- анонимизация
_KZ_VARIANTS = {"а": "аә", "у": "уүұ", "и": "иі", "о": "оө", "к": "кқ", "г": "гғ", "н": "нң",
                "х": "хһ", "ы": "ыі", "е": "её", "ә": "аә", "ү": "уүұ", "ұ": "уүұ", "і": "иі",
                "ө": "оө", "қ": "кқ", "ғ": "гғ", "ң": "нң", "һ": "хһ", "ё": "её"}
_PATR_SUFFIX = ("ович", "евич", "овна", "евна", "ична", "ұлы", "улы", "қызы", "кызы")
_PATR_RE = r"(?:ович|евич|овн|евн|ичн|ұл|ул|қыз|кыз)"
_END = r"[а-яёәғқңөұүһі]{0,3}"
_STOP_TOKENS = {"вера", "надежда", "любовь", "роман", "лев", "мир", "слава", "май", "алма", "ай",
                "нур", "бек", "жан", "ерік", "бақыт", "сая", "дина", "роза", "лилия", "ангелина"}


def _letters(stem: str) -> str:
    out = []
    for ch in stem.lower():
        if ch == "ь":
            out.append("ь?")
        elif ch in _KZ_VARIANTS:
            out.append("[" + _KZ_VARIANTS[ch] + "]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _token_pat(tok: str) -> str:
    low = tok.lower()
    for suf in _PATR_SUFFIX:
        if low.endswith(suf) and len(low) - len(suf) >= 2:
            return _letters(low[: -len(suf)]) + _PATR_RE + _END
    stem = low[:-1] if len(low) > 3 and low[-1] in "аяеоиыйьуюә" else low
    return _letters(stem) + _END


def _wrap(p: str) -> str:
    return r"(?<!\w)" + p + r"(?!\w)"


class Anonymizer:
    """Заменяет ФИО на «Участник N» (включая падежные формы и казахские варианты написания)."""

    def __init__(self, names: list[str], enabled: bool):
        self.enabled = enabled
        self.map: dict[str, str] = {}
        for n in names:
            n = s(n)
            if n and n not in self.map:
                self.map[n] = f"Участник {len(self.map) + 1}"
        self._rules: list[tuple[re.Pattern, str]] = []
        if not enabled:
            return
        full, single = [], []
        for name, repl in self.map.items():
            toks = name.split()
            if _GENERIC_RE.match(name) or any(ch.isdigit() for ch in name):
                full.append((len(toks) + 10, re.compile(_wrap(re.escape(name)), re.I), repl))
                continue
            full.append((len(toks), re.compile(_wrap(r"\s+".join(_token_pat(t) for t in toks)), re.I), repl))
            if len(toks) >= 2:
                for t in toks:
                    if len(t) < 4:
                        continue
                    if t.lower() in _STOP_TOKENS:
                        pat = re.compile(_wrap(re.escape(t)))
                    else:
                        pat = re.compile(_wrap(_token_pat(t)), re.I)
                    single.append((pat, repl))
        full.sort(key=lambda x: -x[0])
        self._rules = [(p, r) for _, p, r in full] + list(single)

    def name(self, n) -> str:
        n = s(n)
        if not self.enabled or not n:
            return n
        return self.map.get(n) or self.text(n)

    def text(self, t) -> str:
        t = s(t)
        if not self.enabled or not t:
            return t
        for pat, repl in self._rules:
            t = pat.sub(repl, t)
        return t


def collect_names(meeting: dict) -> list[str]:
    """Порядок: по первому появлению в стенограмме, затем прочие спикеры, люди, исполнители поручений."""
    names: list[str] = []

    def add(n, kind=None):
        n = s(n)
        if n and n not in names and not is_department(n, kind):
            names.append(n)

    for seg in meeting.get("segments") or []:
        add(seg.get("name"))
    for sp in meeting.get("speakers") or []:
        add(sp.get("name"))
    for p in meeting.get("people") or []:
        add(p.get("name"))
    for t in meeting.get("tasks") or []:
        add(t.get("owner"), t.get("owner_kind"))
        for c in t.get("co_owners") or []:
            add(c)
        add(t.get("issued_by"))
    return names


# ---------------------------------------------------------------- модель документа
def _speaker_pcts(meeting: dict) -> dict[str, int]:
    dur: Counter = Counter()
    for seg in meeting.get("segments") or []:
        try:
            dur[s(seg.get("name")) or s(seg.get("speaker"))] += max(0.0, float(seg.get("end") or 0) - float(seg.get("start") or 0))
        except (TypeError, ValueError):
            pass
    total = sum(dur.values())
    return {k: int(round(v * 100 / total)) for k, v in dur.items()} if total else {}


def _chair(meeting: dict) -> str:
    tasks = meeting.get("tasks") or []
    for t in tasks:
        if t.get("owner_kind") == "chair" and t.get("owner"):
            return s(t["owner"])
    issuers = Counter(s(t.get("issued_by")) for t in tasks if s(t.get("issued_by")))
    if issuers:
        return issuers.most_common(1)[0][0]
    sp = [x for x in meeting.get("speakers") or [] if x.get("name")]
    if sp:
        return s(max(sp, key=lambda x: x.get("pct") or 0)["name"])
    return ""


def normalize_sections(sections) -> set[str]:
    if not sections:
        return set(ALL_SECTIONS)
    if isinstance(sections, str):
        sections = sections.split(",")
    out = {x.strip().lower() for x in sections if x and x.strip()}
    return (out & ALL_SECTIONS) or set(ALL_SECTIONS)


def prepare(meeting: dict, sections, anon: bool) -> dict:
    """Готовит всё текстовое содержимое протокола (уже анонимизированное при anon=True)."""
    sections = normalize_sections(sections)
    if isinstance(anon, str):
        anon = anon.strip().lower() in ("1", "true", "yes", "on")
    anon = bool(anon)
    A = Anonymizer(collect_names(meeting), anon)
    summary = meeting.get("summary") or {}
    if isinstance(summary, str):
        summary = {"short": summary}

    # участники
    pcts = _speaker_pcts(meeting)
    people = []
    for sp in meeting.get("speakers") or []:
        nm = s(sp.get("name")) or s(sp.get("label"))
        pct = sp.get("pct")
        if pct is None:
            pct = pcts.get(nm, pcts.get(s(sp.get("label"))))
        people.append({"name": A.name(nm), "role": A.text(sp.get("role")) or "—",
                       "pct": f"{int(round(float(pct)))}%" if pct not in (None, "") else "—"})
    if not people:
        for p in meeting.get("people") or []:
            people.append({"name": A.name(p.get("name")), "role": A.text(p.get("pos") or p.get("role")) or "—",
                           "pct": f"{pcts[s(p.get('name'))]}%" if s(p.get("name")) in pcts else "—"})
    participant_names = [p["name"] for p in people if p["name"]]

    meta = []
    if meeting.get("date"):
        meta.append(("Дата", fmt_date_long(meeting.get("date"))))
    dur = fmt_duration(meeting.get("duration_s"))
    if dur:
        meta.append(("Длительность", dur))
    langs = fmt_langs(meeting.get("langs"))
    if langs:
        meta.append(("Языки", langs))
    src = SOURCE_LABELS.get(s(meeting.get("source")))
    if src:
        meta.append(("Источник", src))

    tasks = []
    for i, t in enumerate(meeting.get("tasks") or []):
        owner = A.name(t.get("owner")) or "не назначен"
        co = [A.name(c) for c in (t.get("co_owners") or []) if s(c)]
        quote = A.text(t.get("quote"))
        if len(quote) > 240:
            quote = quote[:237].rstrip() + "…"
        tasks.append({
            "n": str(i + 1), "num": s(t.get("num")), "title": A.text(t.get("title")) or "—",
            "quote": quote, "t": fmt_tc(t.get("t")) if t.get("t") else "",
            "owner": owner, "co_owners": co, "due": A.text(fmt_due(t)),
            "done": s(t.get("status")) == "done",
        })

    transcript = []
    for seg in meeting.get("segments") or []:
        nm = s(seg.get("name")) or s(seg.get("speaker")) or "Говорящий"
        transcript.append({"t": fmt_tc(seg.get("t") if seg.get("t") else seg.get("start") or 0),
                           "name": A.name(nm), "lang": s(seg.get("l")), "text": A.text(seg.get("x"))})

    chair = _chair(meeting)
    return {
        "sections": sections,
        "anon": bool(anon),
        "title": "Протокол совещания",
        "org": s(meeting.get("org")),
        "topic": A.text(meeting.get("title")) or "Без темы",
        "meta": meta,
        "participants": participant_names,
        "summary": A.text(summary.get("short")),
        "topics": [(A.text(x.get("label")), fmt_tc(x.get("t")) if x.get("t") else "")
                   for x in summary.get("topics") or [] if isinstance(x, dict) and x.get("label")],
        "numbers": [(s(x.get("value")), A.text(x.get("meaning")))
                    for x in summary.get("numbers") or [] if isinstance(x, dict) and x.get("value")],
        "decisions": [A.text(d) for d in summary.get("decisions") or [] if s(d)],
        "tasks": tasks,
        "transcript": transcript,
        "people": people,
        "chair": A.name(chair) if chair else "",
        "footer": "Протокол сформирован автоматически локальной системой AI Hatshy "
                  f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"
                  + (" · данные обезличены" if anon else ""),
    }
