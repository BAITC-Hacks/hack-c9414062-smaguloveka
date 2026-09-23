"""Детерминированный резолвер сроков поручений (только stdlib).

LLM возвращает deadline_raw (дословную фразу), резолвер превращает её в дату относительно даты совещания.
Поддерживает русский, казахский и «шала» (казахские буквы складываются в русские аналоги: қ→к, ұ→у, і→и ...).

Конвенции (настраиваются константами):
  * «на этой неделе», «до конца недели»      -> пятница текущей недели (WORKWEEK_END)
  * «на следующей неделе»                     -> пятница следующей недели (range: пн..пт)
  * «до пятницы», «к среде»                   -> ближайший такой день СТРОГО после даты совещания
                                                 (в среду «к среде» = следующая среда)
  * «до следующей пятницы»                    -> пятница следующей календарной недели
  * «за N дней/недель/месяцев», «в течение…»  -> дата + N; «рабочих дней» — без сб/вс
  * «до 15 октября», «к пятнадцатому октября», «15.10» -> точная дата (если уже прошла — следующий год)
  * «до конца месяца/квартала/года/октября»   -> последний день периода
  * «после совещания», «на следующем совещании», «по итогам» -> iso=None, kind=event
  * «не указан», пусто                        -> iso=None, kind=unspecified
"""
import datetime as dt
import re
import calendar

WORKWEEK_END = 4  # 0=пн ... 4=пт

KK_FOLD = str.maketrans({"қ": "к", "ғ": "г", "ү": "у", "ұ": "у", "ә": "а", "ө": "о", "і": "и", "ң": "н", "һ": "х", "ё": "е"})

MONTHS = [  # (стем после свёртки, номер месяца); длинные раньше коротких
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("мая", 5), ("май", 5), ("июн", 6), ("июл", 7),
    ("август", 8), ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12),
    ("кантар", 1), ("акпан", 2), ("наурыз", 3), ("сауир", 4), ("мамыр", 5), ("маусым", 6), ("шилде", 7),
    ("тамыз", 8), ("кыркуйек", 9), ("казан", 10), ("караша", 11), ("желтоксан", 12),
]
WEEKDAYS = [  # (регэксп по токену, номер дня)
    (r"понедельник\w*", 0), (r"вторник\w*", 1), (r"сред[аыеуо]\w*", 2), (r"четверг\w*", 3), (r"пятниц\w*", 4),
    (r"суббот\w*", 5), (r"воскресень\w*", 6),
    (r"дуйсенби\w*", 0), (r"сейсенби\w*", 1), (r"сарсенби\w*", 2), (r"бейсенби\w*", 3), (r"жума(?!с)\w*", 4),
    (r"сенби\w*", 5), (r"жексенби\w*", 6),
]
ORD_STEMS = sorted([
    ("перв", 1), ("втор", 2), ("трет", 3), ("четверт", 4), ("пят", 5), ("шест", 6), ("седьм", 7), ("восьм", 8),
    ("девят", 9), ("десят", 10), ("одиннадцат", 11), ("двенадцат", 12), ("тринадцат", 13), ("четырнадцат", 14),
    ("пятнадцат", 15), ("шестнадцат", 16), ("семнадцат", 17), ("восемнадцат", 18), ("девятнадцат", 19),
    ("двадцат", 20), ("тридцат", 30)], key=lambda x: -len(x[0]))
ORD_END = re.compile(r"(ого|ому|ое|ым|ьего|ьему|ье|ий|ый|ой|ая|ую|го)$")
NUM_WORDS = {
    "один": 1, "одну": 1, "одна": 1, "одного": 1, "два": 2, "две": 2, "двух": 2, "три": 3, "трех": 3, "четыре": 4,
    "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "девять": 9,
    "десять": 10, "десяти": 10, "полторы": 1.5, "полтора": 1.5, "пару": 2, "пара": 2,
    # казахский (после свёртки)
    "бир": 1, "еки": 2, "уш": 3, "торт": 4, "бес": 5, "алты": 6, "жети": 7, "сегиз": 8, "тогыз": 9, "он": 10,
}
UNSPECIFIED = re.compile(r"^\s*$|не указан|не назван|без срока|не определ|срок не|^\s*[-–—]\s*$|^нет$|белгиленбеген|мерзими жок")
EVENT = re.compile(r"после |по итогам|по результат|следующ\w* совещ|очередн\w* совещ|по готовност|кейин|нәтижеси|натижеси")
ASAP = re.compile(r"срочно|немедленно|как можно скорее|в кратчайш|asap|тез арада|шугыл")


def _norm(s):
    return (s or "").lower().translate(KK_FOLD).replace(" ", " ").strip()


def _add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _add_business_days(d, n):
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _month_of(tok):
    for stem, m in MONTHS:
        if tok.startswith(stem):
            return m
    return None


def _ordinal_value(tok):
    if not ORD_END.search(tok):
        return None
    for stem, v in ORD_STEMS:
        if tok.startswith(stem):
            return v
    return None


def _week_bounds(d, offset_weeks=0):
    mon = d - dt.timedelta(days=d.weekday()) + dt.timedelta(weeks=offset_weeks)
    return mon, mon + dt.timedelta(days=WORKWEEK_END)


def _res(iso, kind, rule, conf=1.0, start=None):
    out = {"iso": iso.isoformat() if iso else None, "kind": kind, "rule": rule, "confidence": conf}
    if start:
        out["range_start"] = start.isoformat()
    if iso and iso.weekday() >= 5:
        out["weekend"] = True
    return out



# ---- казахские сроки (текст уже свёрнут KK_FOLD: қ→к, ғ→г, ү/ұ→у, ә→а, ө→о, і→и, ң→н) ----
KZ_MONTHS = [("кыркуйек", 9), ("желтоксан", 12), ("караша", 11), ("казан", 10), ("кантар", 1), ("акпан", 2),
             ("наурыз", 3), ("сауир", 4), ("мамыр", 5), ("маусым", 6), ("шилде", 7), ("тамыз", 8)]
KZ_UNITS = [("бир", 1), ("еки", 2), ("уш", 3), ("торт", 4), ("бес", 5), ("алты", 6), ("жети", 7), ("сегиз", 8),
            ("тогыз", 9)]
KZ_TENS = [("отыз", 30), ("жиырма", 20), ("он", 10)]
KZ_WEEKDAYS = [("дуйсенби", 0), ("дейсенби", 0), ("сейсенби", 1), ("сарсенби", 2), ("бейсенби", 3), ("жексенби", 6),
               ("сенби", 5), ("жума", 4)]


def _kz_day(toks):
    """«бірінші», «он бесінші», «жиырма бірінші», «оныншы», «отызыншы» → число 1..31."""
    val = 0
    for t in toks:
        tens = next((v for k, v in KZ_TENS if t == k or (t.startswith(k) and t.endswith(("ншы", "нши", "ыншы", "инши")))), None)
        if tens and t == next(k for k, v in KZ_TENS if v == tens):
            val += tens
            continue
        if tens:
            return val + tens
        unit = next((v for k, v in KZ_UNITS if t.startswith(k) and t.endswith(("ншы", "нши", "ыншы", "инши"))), None)
        if unit:
            return val + unit
        val = 0
    return None


def kz_resolve(s, d0):
    toks = re.findall(r"\w+", s)
    if not toks:
        return None
    if re.search(r"айдын сонына|ай сонына|ай аягына", s):
        last = calendar.monthrange(d0.year, d0.month)[1]
        return _res(dt.date(d0.year, d0.month, last), "end_of_month", "kz: айдың соңына")
    if re.search(r"аптанын сонына|апта сонына", s):
        return _res(d0 + dt.timedelta(days=(4 - d0.weekday()) % 7), "end_of_week", "kz: аптаның соңына")
    if re.search(r"\bертен", s):
        return _res(d0 + dt.timedelta(days=1), "relative", "kz: ертең")
    if re.search(r"\bбугин", s):
        return _res(d0, "relative", "kz: бүгін")
    for i, t in enumerate(toks):  # «он бесінші қазанға»
        mon = next((v for k, v in KZ_MONTHS if t.startswith(k)), None)
        if mon:
            day = _kz_day(toks[max(0, i - 3):i])
            if day:
                y = d0.year + (1 if mon < d0.month - 1 else 0)
                try:
                    return _res(dt.date(y, mon, day), "exact", "kz: день+месяц")
                except ValueError:
                    return None
    for t in toks:  # «жұмаға дейін», «келесі дүйсенбіге»
        wd = next((v for k, v in KZ_WEEKDAYS if t.startswith(k)), None)
        if wd is not None:
            delta = (wd - d0.weekday()) % 7 or 7
            if "келеси" in toks and delta < 3:
                delta += 7
            return _res(d0 + dt.timedelta(days=delta), "weekday", "kz: день недели")
    return None


def resolve(raw, meeting_date):
    """raw: дословная фраза о сроке; meeting_date: date или 'YYYY-MM-DD'. Возвращает dict(iso, kind, rule, confidence)."""
    if isinstance(meeting_date, str):
        meeting_date = dt.date.fromisoformat(meeting_date)
    d0 = meeting_date
    s = _norm(raw)
    toks = re.findall(r"[\w.]+", s)

    if UNSPECIFIED.search(s):
        return _res(None, "unspecified", "не указан")

    if re.search(r"[а-я]*(дейин|шейин|ге дейин|га дейин|сонына|аягына|ертен|бугин)", s) or \
            any(s.find(k) >= 0 for k, _ in KZ_MONTHS):
        r = kz_resolve(s, d0)
        if r:
            return r

    # 1) точные даты: ISO, 15.10(.2026), «15 октября», «к пятнадцатому октября», «15 казанга дейин»
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        return _res(dt.date(int(m[1]), int(m[2]), int(m[3])), "exact", "iso")
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", s)
    if m:
        y = int(m[3]) if m[3] else d0.year
        y = y + 2000 if y < 100 else y
        cand = dt.date(y, int(m[2]), int(m[1]))
        if not m[3] and cand < d0:
            cand = cand.replace(year=y + 1)
        return _res(cand, "exact", "dd.mm")
    for i, tok in enumerate(toks):
        mon = _month_of(tok)
        if mon is None or i == 0:
            continue
        prev = toks[i - 1]
        if prev in ("го", "е", "ое", "шы", "ши", "ыншы", "инши", "нчи", "ого") and i >= 2 and toks[i - 2].isdigit():
            prev = toks[i - 2]  # «15-го октября», «25-шы қазанға»
        day = None
        if prev.isdigit() and 1 <= int(prev) <= 31:
            day = int(prev)
        else:
            v = _ordinal_value(prev)
            if v is not None:
                day = v
                if i >= 2 and toks[i - 2] in ("двадцать", "тридцать") and v < 10:
                    day = v + (20 if toks[i - 2] == "двадцать" else 30)
        if day:
            cand = dt.date(d0.year, mon, day)
            rolled = cand < d0
            if rolled:
                cand = cand.replace(year=d0.year + 1)
            return _res(cand, "exact", "day+month" + (" (след. год)" if rolled else ""), 0.8 if rolled else 1.0)

    if EVENT.search(s):
        return _res(None, "event", "срок привязан к событию")

    # 2) концы периодов
    if re.search(r"конц\w* квартал|токсан сонын", s):
        q_end_month = ((d0.month - 1) // 3 + 1) * 3
        return _res(dt.date(d0.year, q_end_month, calendar.monthrange(d0.year, q_end_month)[1]), "period_end", "конец квартала")
    if re.search(r"конц\w* года|жыл сонын", s):
        return _res(dt.date(d0.year, 12, 31), "period_end", "конец года")
    m = re.search(r"конц\w* (\w+)", s)
    if m and _month_of(m[1]):
        mon = _month_of(m[1]); y = d0.year + (1 if mon < d0.month else 0)
        return _res(dt.date(y, mon, calendar.monthrange(y, mon)[1]), "period_end", "конец месяца (назван)")
    if re.search(r"конц\w* (этого )?месяц|\bай сонын", s):
        return _res(dt.date(d0.year, d0.month, calendar.monthrange(d0.year, d0.month)[1]), "period_end", "конец месяца")

    # 3) недели: следующая / эта
    if re.search(r"следующ\w* недел|келеси апта|будущ\w* недел", s):
        mon, fri = _week_bounds(d0, 1)
        for tok in toks:  # «в среду на следующей неделе», «келесі аптаның сәрсенбісіне»
            for pat, wd in WEEKDAYS:
                if re.fullmatch(pat, tok):
                    return _res(mon + dt.timedelta(days=wd), "weekday", "день следующей недели: " + tok)
        if re.search(r"начал\w* следующ|в понедельник", s):
            return _res(mon, "week", "начало следующей недели", 0.8)
        return _res(fri, "week", "следующая неделя", 0.9, start=mon)
    if re.search(r"(эт\w*|текущ\w*) недел|конц\w* недел|осы апта|апта сонын|до выходных", s):
        _, fri = _week_bounds(d0, 0)
        if fri < d0:  # совещание в выходные
            fri = d0
        return _res(fri, "week", "текущая неделя", 0.9)

    # 4) дни недели
    for i, tok in enumerate(toks):
        for pat, wd in WEEKDAYS:
            if re.fullmatch(pat, tok):
                nxt = any(t.startswith("следующ") or t.startswith("келеси") for t in toks[max(0, i - 2):i])
                if nxt:
                    mon, _ = _week_bounds(d0, 1)
                    return _res(mon + dt.timedelta(days=wd), "weekday", "следующий " + tok, 0.9)
                delta = (wd - d0.weekday()) % 7 or 7
                return _res(d0 + dt.timedelta(days=delta), "weekday", "ближайший " + tok,
                            0.8 if delta == 7 else 1.0)

    # 5) сегодня / завтра / послезавтра
    if re.search(r"послезавтра|бурсигуни", s):
        return _res(d0 + dt.timedelta(days=2), "relative_day", "послезавтра")
    if re.search(r"завтра|\bертен", s):
        return _res(d0 + dt.timedelta(days=1), "relative_day", "завтра")
    if re.search(r"сегодня|конц\w* дня|\bбугин", s):
        return _res(d0, "relative_day", "сегодня")

    # 6) длительности: «за две недели», «в течение 5 рабочих дней», «через месяц», «еки апта ишинде»
    unit_re = r"(рабоч\w* дн\w*|дн\w*|день|сут\w*|недел\w*|месяц\w*|апта\w*|кун\w*|ай|айга|айда|айдын)"
    m = re.search(r"(?:\b(\d+(?:[.,]\d+)?|" + "|".join(sorted(NUM_WORDS, key=len, reverse=True)) + r")\s*)?\b" + unit_re + r"\b", s)
    if m and m[2]:
        n = m[1]
        n = 1 if n is None else (float(n.replace(",", ".")) if n[0].isdigit() else NUM_WORDS[n])
        unit = m[2]
        if unit.startswith("рабоч"):
            return _res(_add_business_days(d0, int(n)), "duration", f"+{int(n)} раб. дн.")
        if unit.startswith(("дн", "день", "сут", "кун")):
            return _res(d0 + dt.timedelta(days=int(n)), "duration", f"+{int(n)} дн.")
        if unit.startswith(("недел", "апта")):
            return _res(d0 + dt.timedelta(days=int(round(n * 7))), "duration", f"+{n} нед.")
        if unit.startswith(("месяц", "ай")):
            if n == 1.5:
                return _res(_add_months(d0, 1) + dt.timedelta(days=15), "duration", "+1.5 мес.")
            return _res(_add_months(d0, int(n)), "duration", f"+{int(n)} мес.")

    if ASAP.search(s):
        return _res(_add_business_days(d0, 1), "asap", "срочно -> следующий рабочий день", 0.5)

    return _res(None, "unparsed", "не распознано", 0.0)


if __name__ == "__main__":
    D = "2026-09-23"  # среда
    CASES = [  # (фраза, ожидаемая дата)
        # протокол №1 (таблицы и диалог)
        ("до 15 октября", "2026-10-15"), ("15 октября", "2026-10-15"), ("до 26 сентября", "2026-09-26"),
        ("26 сентября", "2026-09-26"), ("до 30 сентября", "2026-09-30"), ("30 сентября", "2026-09-30"),
        ("к 20 октября", "2026-10-20"), ("20 октября", "2026-10-20"), ("до пятницы", "2026-09-25"),
        ("Пятница", "2026-09-25"), ("к пятнадцатому октября", "2026-10-15"), ("Три недели, но не больше", "2026-10-14"),
        ("Две недели", "2026-10-07"), ("на следующей неделе", "2026-10-02"), ("Следующая неделя", "2026-10-02"),
        ("на этой неделе", "2026-09-25"), ("Текущая неделя", "2026-09-25"), ("к среде", "2026-09-30"),
        ("Среда", "2026-09-30"), ("до конца квартала", "2026-09-30"), ("до конца недели", "2026-09-25"),
        ("после проведения", None), ("на следующем совещании", None),
        # протокол №2
        ("До конца недели", "2026-09-25"), ("за две недели", "2026-10-07"), ("2 недели", "2026-10-07"),
        ("за неделю", "2026-09-30"), ("1 неделя (смета)", "2026-09-30"), ("за месяц", "2026-10-23"),
        ("После совещания с подрядчиками", None), ("по итогам этого совещания", None), ("Не указан", None),
        ("пять рабочих дней после выполнения работ", None),  # не срок поручения, а условие договора -> event
        # дополнительные
        ("в течение пяти рабочих дней", "2026-09-30"), ("до 15.10", "2026-10-15"), ("к следующей пятнице", "2026-10-02"),
        ("в понедельник", "2026-09-28"), ("послезавтра", "2026-09-25"), ("завтра", "2026-09-24"),
        ("до конца года", "2026-12-31"), ("до конца октября", "2026-10-31"), ("до конца месяца", "2026-09-30"),
        ("через три дня", "2026-09-26"), ("срочно", "2026-09-24"), ("к двадцать пятому сентября", "2026-09-25"),
        # казахский / шала
        ("ертеңге дейін", "2026-09-24"), ("ертең", "2026-09-24"), ("жұмаға дейін", "2026-09-25"),
        ("келесі аптада", "2026-10-02"), ("екі апта ішінде", "2026-10-07"), ("15 қазанға дейін", "2026-10-15"),
        ("осы аптада", "2026-09-25"), ("бір ай ішінде", "2026-10-23"), ("сәрсенбіге дейін", "2026-09-30"),
        ("дүйсенбіге дейін", "2026-09-28"), ("апта соңына дейін", "2026-09-25"), ("30 қыркүйекке дейін", "2026-09-30"),
        ("жума күні", "2026-09-25"),  # STT без казахских букв
        ("келесі аптаның сәрсенбісіне", "2026-09-30"), ("в среду на следующей неделе", "2026-09-30"),
        ("до вторника", "2026-09-29"), ("ertең түске дейін".replace("ert", "ерт"), "2026-09-24"), ("сегодня же", "2026-09-23"),
        # ложные срабатывания на целых фразах (резолвер применяется и к цитатам в верификаторе)
        ("Айнур Каировна, раз вы подняли вопрос, возьмите на себя связь с юридическим департаментом", None),
        ("2 недели", "2026-10-07"), ("в течение 10 дней", "2026-10-03"),
        ("25-шы қазаңға дейін", "2026-10-25"), ("до 15-го октября", "2026-10-15"),  # формы из реального STT
    ]
    ok = 0
    for raw, exp in CASES:
        r = resolve(raw, D)
        good = r["iso"] == exp
        ok += good
        print(f"{'OK ' if good else 'ERR'} {raw!r:45} -> {r['iso']!s:10} [{r['kind']}: {r['rule']}]" + ("" if good else f"  expected {exp}"))
    print(f"\n{ok}/{len(CASES)} passed")
