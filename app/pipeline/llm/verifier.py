"""Проход extractor -> verifier.
1) Детерминированные проверки (без LLM): цитата дословно есть в транскрипте (иначе fuzzy-поиск и замена),
   цитата принадлежит реплике source_speaker_label, ответственный разрешим (участник / подразделение / имя из текста),
   срок разрешим резолвером, дедупликация.
2) LLM-проверка: модель получает пункт + локальный контекст (±2 реплики вокруг цитаты) и выносит вердикт.
python3 verifier.py runs/m1_v1_nothink.json inputs/m1_transcript.txt runs/m1_verified.json [--no-llm]"""
import json, re, sys, difflib, time
from .deadline_resolver import resolve
from . import extract

MEETING_DATE = "2026-09-23"
ORG_UNIT = re.compile(r"департамент|отдел|служб|управлени|бөлім|болим|юрист|заңгер|комисси|дирекц", re.I)


def norm(s):
    return re.sub(r"[^\w%]+", " ", (s or "").lower().replace("ё", "е")).strip()


def turns_of(transcript):
    out = []
    for line in transcript.splitlines():
        m = re.match(r"\[(SPEAKER_\d+)\]\s*(.*)", line)
        if m:
            out.append((m[1], m[2]))
    return out


def find_quote(quote, turns):
    """Возвращает (turn_idx, exact_bool, best_fragment, ratio). Цитаты, склеенные через «...», проверяются по частям."""
    parts = [x for x in re.split(r"\.\.\.|…", quote or "") if norm(x)]
    if len(parts) > 1:
        res = [find_quote(x, turns) for x in parts]
        worst = min(res, key=lambda r: r[3])
        return res[0][0], all(r[1] for r in res), quote, worst[3]
    qn = norm(quote)
    if not qn:
        return None, False, None, 0.0
    for i, (_, t) in enumerate(turns):
        if qn in norm(t):
            return i, True, quote, 1.0
    best = (None, False, None, 0.0)
    qw = qn.split()
    for i, (_, t) in enumerate(turns):
        tw = norm(t).split()
        L = len(qw)
        for j in range(0, max(1, len(tw) - L + 1)):
            win = " ".join(tw[j:j + L])
            r = difflib.SequenceMatcher(None, qn, win).ratio()
            if r > best[3]:
                best = (i, False, win, r)
    return best


def assignee_status(item, participants, transcript_n):
    a = norm(item.get("assignee"))
    if not a:
        return "empty"
    names = [norm(p.get("name")) for p in participants if p.get("name")]
    first = a.split()[0]
    if any(first and first in n for n in names):
        return "participant"
    if ORG_UNIT.search(a):
        return "org_unit"
    if first and len(first) > 2 and first[:5] in transcript_n:
        return "named_in_text"
    return "unresolved"


VERIFY_SYSTEM = """Ты — контролёр качества протокола совещания. Тебе дают фрагмент транскрипта (реплики с метками SPEAKER_xx) и список пунктов-кандидатов в поручения, извлечённых другой моделью. Для КАЖДОГО пункта проверь по фрагменту:
- is_action_item: это действительно поручение/обязательство (конкретное действие, которое кто-то должен выполнить), а не доклад, рассуждение или условный план;
- assignee_ok: ответственный указан верно (кому адресовано поручение или кто взял обязательство); если неверно — дай corrected_assignee так, как он назван в тексте;
- deadline_ok: deadline_raw соответствует сказанному в тексте; если нет — дай corrected_deadline_raw (дословно из текста или «не указан»);
- duplicate_of: номер более раннего пункта, если это то же самое поручение, иначе null;
- verdict: keep (оставить), fix (оставить с исправлениями), drop (удалить);
- reason: кратко, по-русски.
Отвечай строго JSON по схеме."""

VERIFY_SCHEMA = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
    "n": {"type": "integer"}, "is_action_item": {"type": "boolean"}, "assignee_ok": {"type": "boolean"},
    "corrected_assignee": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "deadline_ok": {"type": "boolean"},
    "corrected_deadline_raw": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "duplicate_of": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "verdict": {"type": "string", "enum": ["keep", "fix", "drop"]}, "reason": {"type": "string"}},
    "required": ["n", "is_action_item", "assignee_ok", "corrected_assignee", "deadline_ok", "corrected_deadline_raw",
                 "duplicate_of", "verdict", "reason"]}}}, "required": ["verdicts"]}


def verify(extraction, transcript, use_llm=True, ctx=2, think=False, meeting_date=None):
    global MEETING_DATE
    if meeting_date:
        MEETING_DATE = meeting_date
    turns = turns_of(transcript)
    tn = norm(transcript)
    parts = extraction.get("participants", [])
    items = extraction.get("action_items", [])
    checked = []
    for k, it in enumerate(items):
        ti, exact, frag, ratio = find_quote(it.get("source_quote", ""), turns)
        flags = []
        if ti is None or ratio < 0.6:
            flags.append("quote_not_found")
        elif not exact:
            flags.append(f"quote_fuzzy:{ratio:.2f}")
        if ti is not None and turns[ti][0] != it.get("source_speaker_label"):
            flags.append(f"quote_speaker_mismatch:{turns[ti][0]}")
        ast = assignee_status(it, parts, tn)
        if ast == "unresolved":
            flags.append("assignee_unresolved")
        raw = it.get("deadline_raw") or "не указан"
        r = resolve(raw, MEETING_DATE)
        if r["kind"] == "unparsed":
            flags.append("deadline_unparsed")
        if r["kind"] not in ("unspecified",) and norm(raw)[:12] not in tn:
            flags.append("deadline_raw_not_in_text")
        elif r["kind"] not in ("unspecified",) and ti is not None:
            raw_n = norm(raw)[:12]
            here = norm(it.get("source_quote", "")) + " " + norm(turns[ti][1])
            if raw_n not in here:
                q_r = resolve(it.get("source_quote", ""), MEETING_DATE)
                near = " ".join(norm(turns[j][1]) for j in (ti - 1, ti + 1) if 0 <= j < len(turns))
                if q_r["kind"] not in ("unparsed", "unspecified"):
                    flags.append(f"deadline_conflict_with_quote:{q_r['iso']}")
                elif raw_n not in near:
                    flags.append("deadline_not_near_quote")
        # согласованность assignee <-> assignee_speaker_label (детерминированная починка)
        lab2name = {p["speaker_label"]: norm(p.get("name")) for p in parts}
        a_first = (norm(it.get("assignee")).split() or [""])[0][:5]
        lab = it.get("assignee_speaker_label")
        match_lab = next((l for l, n in lab2name.items() if n and a_first and n.startswith(a_first)), None)
        if lab != match_lab:
            flags.append(f"assignee_label_fixed:{lab}->{match_lab}")
            it = {**it, "assignee_speaker_label": match_lab}
        checked.append({**it, "_n": k + 1, "_turn": ti, "_quote_exact": exact, "_assignee_status": ast,
                        "deadline_resolved": r["iso"], "deadline_kind": r["kind"], "_flags": flags})

    metrics = None
    if use_llm and checked:
        # локальный контекст: реплики вокруг всех цитат (для длинных совещаний — только они, а не весь транскрипт)
        idx = set()
        for c in checked:
            if c["_turn"] is not None:
                idx.update(range(max(0, c["_turn"] - ctx), min(len(turns), c["_turn"] + ctx + 1)))
        frag = "\n".join(f"[{turns[i][0]}] {turns[i][1]}" for i in sorted(idx))
        names = "; ".join(f"{p['speaker_label']} = {p.get('name')}" for p in parts)
        cand = "\n".join(
            f"{c['_n']}. {c.get('description')} | ответственный: {c.get('assignee')} | срок: {c.get('deadline_raw')} | цитата: «{c.get('source_quote')}»"
            for c in checked)
        user = f"Участники: {names}\n\nФрагмент транскрипта:\n{frag}\n\nПункты-кандидаты:\n{cand}"
        content, thinking, metrics, _ = extract.call(
            [{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": user}], VERIFY_SCHEMA,
            think=think)
        try:
            verdicts = {v["n"]: v for v in json.loads(content)["verdicts"]}
        except Exception as e:
            verdicts = {}
            metrics["verify_parse_error"] = str(e)
        for c in checked:
            v = verdicts.get(c["_n"])
            c["_llm"] = v
    # итоговое решение
    final = []
    for c in checked:
        v = c.get("_llm") or {}
        drop = ("quote_not_found" in c["_flags"] and c["_assignee_status"] == "unresolved") or v.get("verdict") == "drop" \
            or v.get("duplicate_of")
        if v.get("verdict") == "fix":
            if v.get("corrected_assignee"):
                c["assignee_before"] = c["assignee"]; c["assignee"] = v["corrected_assignee"]
            if v.get("corrected_deadline_raw"):
                c["deadline_raw_before"] = c["deadline_raw"]; c["deadline_raw"] = v["corrected_deadline_raw"]
                r = resolve(c["deadline_raw"], MEETING_DATE); c["deadline_resolved"] = r["iso"]; c["deadline_kind"] = r["kind"]
        hard = [f for f in c["_flags"] if not f.startswith("assignee_label_fixed")]
        c["_status"] = "dropped" if drop else ("needs_review" if hard else ("auto_fixed" if c["_flags"] else "ok"))
        if v.get("verdict") == "fix" and c["_status"] == "ok":
            c["_status"] = "auto_fixed"
        final.append(c)
    return final, metrics


if __name__ == "__main__":
    src, trp, out = sys.argv[1:4]
    use_llm = "--no-llm" not in sys.argv
    ext = json.load(open(src, encoding="utf-8"))["result"]
    tr = open(trp, encoding="utf-8").read()
    t0 = time.time()
    final, metrics = verify(ext, tr, use_llm=use_llm, think="--think" in sys.argv)
    json.dump({"metrics": metrics, "wall_s": round(time.time() - t0, 1), "items": final}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    for c in final:
        v = c.get("_llm") or {}
        print(c["_n"], c["_status"], c["assignee"], "|", c["deadline_raw"], "->", c["deadline_resolved"], c["_flags"],
              v.get("verdict"), (v.get("reason") or "")[:100])
    print(metrics)
