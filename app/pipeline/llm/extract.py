"""Извлечение протокола локальной LLM через Ollama /api/chat со structured outputs (format = JSON Schema).
Только stdlib. Запуск: python3 extract.py <transcript.txt> <out.json> [--think] [--date 2026-09-23] [--variant v1]"""
import json, re, sys, time, argparse, urllib.request, datetime as dt

from ...config import OLLAMA_URL, LLM_MODEL
OLLAMA = OLLAMA_URL.rstrip("/") + "/api/chat"
MODEL = LLM_MODEL
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
CATEGORIES = ["производство", "финансы", "закупки и поставки", "договоры и юридические вопросы",
              "охрана труда и безопасность", "персонал и обучение", "отчётность", "другое"]


def build_schema(labels):
    lab = {"type": "string", "enum": labels}
    lab_or_null = {"anyOf": [lab, {"type": "null"}]}
    s_or_null = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "participants": {"type": "array", "items": {"type": "object", "properties": {
                "speaker_label": lab,
                "evidence": {"type": "string"},
                "name": s_or_null,
                "role": s_or_null,
                "confidence": {"type": "number"},
            }, "required": ["speaker_label", "evidence", "name", "role", "confidence"]}},
            "action_items": {"type": "array", "items": {"type": "object", "properties": {
                "source_speaker_label": lab,
                "source_quote": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string"},
                "assignee_speaker_label": lab_or_null,
                "co_assignees": {"type": "array", "items": {"type": "string"}},
                "issued_by": s_or_null,
                "deadline_raw": {"type": "string"},
                "deadline_iso": s_or_null,
                "category": {"type": "string", "enum": CATEGORIES},
                "urgency": {"type": "string", "enum": ["высокая", "средняя", "низкая"]},
            }, "required": ["source_speaker_label", "source_quote", "description", "assignee", "assignee_speaker_label",
                            "co_assignees", "issued_by", "deadline_raw", "deadline_iso", "category", "urgency"]}},
            "summary": {"type": "object", "properties": {
                "topics": {"type": "array", "items": {"type": "string"}},
                "key_decisions": {"type": "array", "items": {"type": "string"}},
                "key_numbers": {"type": "array", "items": {"type": "object", "properties": {
                    "value": {"type": "string"}, "meaning": {"type": "string"}}, "required": ["value", "meaning"]}},
                "short_summary": {"type": "string"},
            }, "required": ["topics", "key_decisions", "key_numbers", "short_summary"]},
        },
        "required": ["participants", "action_items", "summary"],
    }


SYSTEM_V1 = """Ты — ИИ-секретарь, который составляет протокол совещания по транскрипту.
Транскрипт получен распознаванием речи и диаризацией: каждая реплика начинается с метки говорящего [SPEAKER_xx]. Настоящие имена участников в метках скрыты. Совещание может идти на русском, казахском или смешанном языке, но весь ответ пиши на русском.

Дата совещания: {date} ({weekday}).

Сделай три вещи.

1. УЧАСТНИКИ (participants). Для каждой метки SPEAKER_xx определи имя и роль.
- Имя бери из обращений: если один говорящий обращается к человеку по имени («Тимур Болатович, что у вас?», «Начнём с Ботагоз Нурлановны»), а СЛЕДУЮЩУЮ реплику произносит другая метка и отвечает по существу — эта метка и есть названный человек. Имя пиши в именительном падеже («Ботагоз Нурлановна», а не «Ботагоз Нурлановны»).
- Тот, кто открывает совещание, даёт слово и раздаёт поручения, — председатель; его имя может ни разу не прозвучать, тогда name = null. Если кто-то обращается к председателю по имени — используй это имя.
- role — должность или зона ответственности, только если она следует из текста (например, «курирует подрядчиков», «инвестиции»), иначе null.
- evidence — короткая дословная цитата, на которой основан вывод; confidence — от 0 до 1.

2. ПОРУЧЕНИЯ (action_items). Извлеки ВСЕ поручения, а не только итоговый список:
- поручения, зачитанные председателем списком («Первое: … ответственный …, срок …»);
- поручения, данные по ходу обсуждения («разберитесь…», «свяжитесь…», «проводите…», «пусть Ерлан подготовит…»);
- обязательства, взятые участниками («подготовлю…», «запрошу…, к среде будет ответ»).
Не включай: доклады о текущем положении, неподтверждённые предложения, условные планы («если нарушают — расторгаем»), общие рассуждения.
Если одно и то же поручение повторяется (например, в итоговом перечислении в конце) — это ОДИН пункт; бери самый точный срок. Разные действия одного человека — разные пункты.
Поля:
- source_speaker_label — метка того, кто дал поручение или взял обязательство;
- source_quote — ДОСЛОВНЫЙ фрагмент транскрипта (скопируй без изменений, 1–2 предложения), где дано поручение;
- description — суть поручения, начинается с глагола в инфинитиве («Подготовить…», «Провести…»);
- assignee — ответственный так, как он назван в тексте (имя-отчество в именительном падеже, имя или подразделение, например «Юридический департамент»); исполнитель может не быть участником совещания (например, «Ерлан, юрист департамента»);
- assignee_speaker_label — метка ответственного, если он участник совещания, иначе null;
- co_assignees — соисполнители, если есть;
- issued_by — кто дал поручение (имя или метка);
- deadline_raw — ДОСЛОВНАЯ фраза о сроке из текста («до пятницы», «на следующей неделе», «за две недели», «до 15 октября»); если срок не назван — «не указан»;
- deadline_iso — дата ГГГГ-ММ-ДД, вычисленная от даты совещания: «до пятницы» — ближайшая пятница; «на этой неделе»/«до конца недели» — пятница текущей недели; «на следующей неделе» — пятница следующей недели; «за N недель» — дата совещания + N×7 дней; день недели («к среде») — ближайший будущий такой день после даты совещания; если срок не назван или зависит от события — null;
- category и urgency — классификация по направлению и срочности (высокая — срок до 7 дней или важный риск).

3. САММАРИ (summary): темы (topics), ключевые решения (key_decisions), ключевые цифры с пояснением (key_numbers), краткое резюме из 3–5 предложений (short_summary).

Не выдумывай ничего, чего нет в транскрипте. Ответ — строго JSON по заданной схеме."""


def build_schema_v2(labels):
    """Гибрид: LLM не считает даты и срочность (это делает резолвер), меньше выходных токенов."""
    lab = {"type": "string", "enum": labels}
    lab_or_null = {"anyOf": [lab, {"type": "null"}]}
    s_or_null = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "participants": {"type": "array", "items": {"type": "object", "properties": {
                "speaker_label": lab, "name": s_or_null, "role": s_or_null, "confidence": {"type": "number"},
                "evidence": {"type": "string"},
            }, "required": ["speaker_label", "name", "role", "confidence", "evidence"]}},
            "action_items": {"type": "array", "items": {"type": "object", "properties": {
                "source_speaker_label": lab,
                "source_quote": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string"},
                "assignee_speaker_label": lab_or_null,
                "co_assignees": {"type": "array", "items": {"type": "string"}},
                "deadline_raw": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
            }, "required": ["source_speaker_label", "source_quote", "description", "assignee", "assignee_speaker_label",
                            "co_assignees", "deadline_raw", "category"]}},
            "summary": {"type": "object", "properties": {
                "topics": {"type": "array", "items": {"type": "string"}},
                "key_decisions": {"type": "array", "items": {"type": "string"}},
                "key_numbers": {"type": "array", "items": {"type": "object", "properties": {
                    "value": {"type": "string"}, "meaning": {"type": "string"}}, "required": ["value", "meaning"]}},
                "short_summary": {"type": "string"},
            }, "required": ["topics", "key_decisions", "key_numbers", "short_summary"]},
        },
        "required": ["participants", "action_items", "summary"],
    }


SYSTEM_V2 = """Ты — ИИ-секретарь. Составь структурированный протокол совещания по транскрипту.
Транскрипт получен распознаванием речи и диаризацией: каждая реплика начинается с метки говорящего [SPEAKER_xx]; настоящие имена в метках скрыты. Речь может быть на русском, казахском или смешанной («шала»), но ВЕСЬ ответ пиши на русском. Дата совещания: {date} ({weekday}).

1. УЧАСТНИКИ (participants) — по одному элементу на каждую метку.
- Имя определяй по обращениям. Если говорящий A обращается к человеку по имени («Марат Сейтович, что у вас?», «Начнём с Жанар Болатовны»), а следующую реплику произносит другая метка B и отвечает по существу — B и есть этот человек.
- Ведущий (председатель) открывает совещание, даёт слово и раздаёт поручения. Его имя обычно звучит, когда к нему обращается другой участник («Марат Сейтович, можно добавить?»): такое обращение внутри реплики B, адресованное ведущему, даёт имя ведущего. Просмотри ВЕСЬ транскрипт в поисках таких обращений. Если имени нет — name = null.
- Имя — в именительном падеже, как в тексте (имя-отчество или имя). role — должность или зона ответственности, только если она следует из текста, иначе null. evidence — короткая дословная цитата-основание. confidence — 0..1.

2. ПОРУЧЕНИЯ (action_items) — извлеки ВСЕ:
- зачитанные ведущим списком («Первое: … ответственный …, срок …»);
- данные по ходу обсуждения («разберитесь…», «свяжитесь…», «проводите…», «пусть Бекзат подготовит…»);
- обязательства, взятые участниками («подготовлю…», «отправлю к понедельнику»).
Не включай доклады о текущем положении, неподтверждённые предложения, условные планы («если не исправят — расторгаем»), рассуждения.
Одно поручение, повторённое несколько раз (например, в итоговом перечислении в конце), — это ОДИН пункт; бери самую точную формулировку срока. Разные действия — разные пункты.
Поля:
- source_speaker_label — метка того, кто дал поручение (или взял обязательство);
- source_quote — одна НЕПРЕРЫВНАЯ дословная цитата из транскрипта (1–2 предложения, без многоточий и пересказа), где дано поручение;
- description — суть поручения, начинается с глагола в инфинитиве («Подготовить…», «Провести…»);
- assignee — ответственный, как он назван в тексте, в именительном падеже (имя-отчество, имя или подразделение: «Юридический департамент»). Исполнитель может не быть участником совещания («Бекзат, юрист отдела»); если поручение передано другому человеку («пусть Бекзат подготовит»), ответственный — он;
- assignee_speaker_label — метка ответственного, если он участник совещания, иначе null;
- co_assignees — соисполнители, если есть, иначе пустой список;
- deadline_raw — срок ДОСЛОВНО, как сказано в тексте («до пятницы», «на следующей неделе», «за две недели», «к 15 ноября», «ертеңге дейін»); если срок назван и в поручении, и в итоговом перечислении — бери более конкретный; если срока нет — «не указан». Даты НЕ вычисляй;
- category — направление поручения.

3. САММАРИ (summary): topics — темы; key_decisions — ключевые решения; key_numbers — все значимые цифры с пояснением (проценты, количества, сроки проектов); short_summary — 3–5 предложений.

Ничего не выдумывай. Ответ — строго JSON по схеме."""


def build_schema_v3(labels):
    """Финальная схема: порядок полей = порядок рассуждения (сначала цитата/основание, потом выводы);
    сроки и срочность считает детерминированный код, LLM отдаёт только deadline_raw."""
    lab = {"type": "string", "enum": labels}
    lab_or_null = {"anyOf": [lab, {"type": "null"}]}
    s_or_null = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "participants": {"type": "array", "items": {"type": "object", "properties": {
                "speaker_label": lab,
                "evidence": {"type": "string"},
                "name": s_or_null,
                "role": s_or_null,
                "confidence": {"type": "number"},
            }, "required": ["speaker_label", "evidence", "name", "role", "confidence"]}},
            "action_items": {"type": "array", "items": {"type": "object", "properties": {
                "source_speaker_label": lab,
                "source_quote": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string"},
                "assignee_speaker_label": lab_or_null,
                "co_assignees": {"type": "array", "items": {"type": "string"}},
                "issued_by": s_or_null,
                "deadline_raw": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
            }, "required": ["source_speaker_label", "source_quote", "description", "assignee", "assignee_speaker_label",
                            "co_assignees", "issued_by", "deadline_raw", "category"]}},
            "summary": {"type": "object", "properties": {
                "topics": {"type": "array", "items": {"type": "string"}},
                "key_decisions": {"type": "array", "items": {"type": "string"}},
                "key_numbers": {"type": "array", "items": {"type": "object", "properties": {
                    "value": {"type": "string"}, "meaning": {"type": "string"}}, "required": ["value", "meaning"]}},
                "short_summary": {"type": "string"},
            }, "required": ["topics", "key_decisions", "key_numbers", "short_summary"]},
        },
        "required": ["participants", "action_items", "summary"],
    }


SYSTEM_V3 = """Ты — ИИ-секретарь, который составляет протокол совещания по транскрипту.
Транскрипт получен распознаванием речи и диаризацией: каждая реплика начинается с метки говорящего [SPEAKER_xx]. Настоящие имена участников в метках скрыты. Совещание может идти на русском, казахском или смешанном языке, но весь ответ пиши на русском.

Дата совещания: {date} ({weekday}).

Сделай три вещи.

1. УЧАСТНИКИ (participants). Для каждой метки SPEAKER_xx определи имя и роль.
- Имя бери из обращений: если один говорящий обращается к человеку по имени («Марат Сейтович, что у вас?», «Начнём с Жанар Болатовны»), а СЛЕДУЮЩУЮ реплику произносит другая метка и отвечает по существу — эта метка и есть названный человек. Имя пиши в именительном падеже («Жанар Болатовна», а не «Жанар Болатовны»).
- Обращение в начале собственной реплики («Марат Сейтович, можно добавить?») называет СОБЕСЕДНИКА, а не говорящего.
- Тот, кто открывает совещание, даёт слово и раздаёт поручения, — председатель; если к нему никто не обращается по имени, name = null.
- role — должность или зона ответственности, только если она следует из текста, иначе null.
- evidence — короткая дословная цитата, на которой основан вывод; confidence — от 0 до 1. Если имени нет, name = null (не строка).

2. ПОРУЧЕНИЯ (action_items). Извлеки ВСЕ поручения, а не только итоговый список:
- поручения, зачитанные председателем списком («Первое: … ответственный …, срок …»);
- поручения, данные по ходу обсуждения («разберитесь…», «свяжитесь…», «проводите…», «пусть Бекзат подготовит…»);
- обязательства, взятые участниками («подготовлю…», «отправлю к понедельнику»).
Не включай: доклады о текущем положении, неподтверждённые предложения, условные планы («если не исправят — расторгаем»), общие рассуждения.
Один пункт — один ответственный. Если в одной фразе поручения даны разным людям («пусть Бекзат подготовит письмо, а вы найдите подрядчика») — это ДВА пункта с разными ответственными.
Если одно и то же поручение повторяется (например, в итоговом перечислении в конце) — это ОДИН пункт. Разные действия одного человека — разные пункты.
Поля:
- source_speaker_label — метка того, кто дал поручение или взял обязательство;
- source_quote — одна НЕПРЕРЫВНАЯ дословная цитата из транскрипта (1–2 предложения, без многоточий), где дано поручение;
- description — суть поручения, начинается с глагола в инфинитиве («Подготовить…», «Провести…»);
- assignee — ответственный так, как он назван в тексте (имя-отчество в именительном падеже, имя или подразделение, например «Юридический департамент»); исполнитель может не быть участником совещания;
- assignee_speaker_label — метка ответственного, если он участник совещания, иначе null;
- co_assignees — соисполнители, если есть;
- issued_by — кто дал поручение (имя или метка);
- deadline_raw — срок ДОСЛОВНО, как сказано («до пятницы», «на следующей неделе», «за две недели», «к 15 ноября», «ертеңге дейін»); если срок назван и в поручении, и в итоговом перечислении — более конкретный; если срока нет — «не указан». Даты не вычисляй;
- category — направление поручения.

3. САММАРИ (summary): темы (topics), ключевые решения (key_decisions), все значимые цифры с пояснением (key_numbers: проценты, количества, сроки), краткое резюме из 3–5 предложений (short_summary).

Не выдумывай ничего, чего нет в транскрипте. Ответ — строго JSON по заданной схеме."""


def _call_openai(messages, schema, cfg, timeout=900):
    """OpenAI-совместимый /chat/completions (OpenAI или свой vLLM). Структурированный вывод по JSON-схеме;
    если сервер/модель не поддерживает json_schema или temperature — повтор с упрощёнными параметрами."""
    import urllib.error
    url = cfg["openai_base_url"].rstrip("/") + "/chat/completions"
    variants = [
        {"temperature": 0, "response_format": {"type": "json_schema",
                                               "json_schema": {"name": "meeting_protocol", "schema": schema}}},
        {"response_format": {"type": "json_schema", "json_schema": {"name": "meeting_protocol", "schema": schema}}},
        {"response_format": {"type": "json_object"}},
    ]
    t0 = time.time()
    last_err = None
    for extra in variants:
        body = {"model": cfg["openai_model"], "messages": messages, **extra}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {cfg['openai_key']}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}"
            if e.code == 400:
                continue
            raise RuntimeError(last_err)
        content = resp["choices"][0]["message"].get("content") or ""
        u = resp.get("usage") or {}
        wall = time.time() - t0
        metrics = {"endpoint": "openai", "provider": "openai", "model": cfg["openai_model"], "think": False,
                   "wall_s": round(wall, 1), "prompt_tokens": u.get("prompt_tokens"), "eval_count": u.get("completion_tokens"),
                   "gen_tok_per_s": round((u.get("completion_tokens") or 0) / wall, 1) if wall else None}
        return content, None, metrics, resp
    raise RuntimeError(f"OpenAI API отклонил запрос: {last_err}")


def call(messages, schema, think=False, num_ctx=8192, timeout=3600, options=None):
    from ...llm_settings import get as llm_cfg
    cfg = llm_cfg()
    if cfg["provider"] == "openai":
        return _call_openai(messages, schema, cfg)
    model = cfg.get("ollama_model") or MODEL
    """ВАЖНО (проверено на Ollama 0.21.0 + gemma4): в /api/chat при think=false параметр format ИГНОРИРУЕТСЯ
    (модель отвечает свободным текстом). Поэтому: think=False -> /api/generate (format соблюдается),
    think=True -> /api/chat (грамматика применяется после блока рассуждений). Стриминг — чтобы честно
    посчитать токены рассуждений (eval_count их, судя по всему, не включает)."""
    opts = {"temperature": 0, "seed": 42, "num_ctx": num_ctx, **(options or {})}
    if think:
        url = OLLAMA
        body = {"model": model, "messages": messages, "stream": True, "format": schema, "think": True,
                "keep_alive": "10m", "options": opts}
    else:
        url = OLLAMA.replace("/api/chat", "/api/generate")
        sys_msg = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        user_msg = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
        body = {"model": model, "system": sys_msg, "prompt": user_msg, "stream": True, "format": schema,
                "think": False, "keep_alive": "10m", "options": opts}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time(); ttft = None; content = []; thinking = []; n_think = n_content = 0; last = {}
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            if not line.strip():
                continue
            ch = json.loads(line)
            if think:
                msg = ch.get("message", {})
                c, th = msg.get("content", ""), msg.get("thinking", "")
            else:
                c, th = ch.get("response", ""), ch.get("thinking", "")
            if (c or th) and ttft is None:
                ttft = time.time() - t0
            if th:
                thinking.append(th); n_think += 1
            if c:
                content.append(c); n_content += 1
            if ch.get("done"):
                last = ch
    wall = time.time() - t0
    metrics = {
        "endpoint": url.rsplit("/", 1)[-1], "provider": "ollama", "model": model, "think": think,
        "wall_s": round(wall, 1),
        "load_s": round(last.get("load_duration", 0) / 1e9, 1),
        "ttft_s": round(ttft or 0, 1),
        "prompt_tokens": last.get("prompt_eval_count"),
        "prompt_s": round(last.get("prompt_eval_duration", 0) / 1e9, 1),
        "eval_count": last.get("eval_count"),
        "gen_s": round(last.get("eval_duration", 0) / 1e9, 1),
        "think_chunks": n_think, "content_chunks": n_content,
    }
    if metrics["prompt_s"]:
        metrics["prompt_tok_per_s"] = round(metrics["prompt_tokens"] / metrics["prompt_s"], 1)
    if metrics["gen_s"]:
        metrics["gen_tok_per_s"] = round(metrics["eval_count"] / metrics["gen_s"], 1)
    # время генерации без загрузки модели и без ожидания в очереди нельзя разделить точно; wall - load - prompt
    metrics["net_s"] = round(wall - metrics["load_s"], 1)
    return "".join(content), "".join(thinking) or None, metrics, last


def labels_of(transcript):
    return sorted(set(re.findall(r"\[(SPEAKER_\d+)\]", transcript)))


def run(transcript, date, think=False, variant="v1", num_ctx=8192):
    d = dt.date.fromisoformat(date)
    system = {"v1": SYSTEM_V1, "v2": SYSTEM_V2, "v3": SYSTEM_V3}[variant]
    sys_prompt = system.format(date=date, weekday=WEEKDAYS_RU[d.weekday()])
    schema = {"v1": build_schema, "v2": build_schema_v2, "v3": build_schema_v3}[variant](labels_of(transcript))
    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": "Транскрипт совещания:\n\n" + transcript}]
    content, thinking, metrics, _ = call(msgs, schema, think=think, num_ctx=num_ctx)
    try:
        data = json.loads(content)
        metrics["json_ok"] = True
    except Exception as e:
        data = {"_raw": content, "_error": str(e)}
        metrics["json_ok"] = False
    return data, thinking, metrics


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript"); ap.add_argument("out")
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--date", default="2026-09-23")
    ap.add_argument("--num_ctx", type=int, default=8192)
    ap.add_argument("--variant", default="v1")
    a = ap.parse_args()
    tr = open(a.transcript, encoding="utf-8").read()
    data, thinking, metrics = run(tr, a.date, think=a.think, variant=a.variant, num_ctx=a.num_ctx)
    json.dump({"metrics": metrics, "thinking": thinking, "result": data}, open(a.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(metrics, ensure_ascii=False))
