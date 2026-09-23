"""Сборка протокола из ответа LLM: имена говорящих (обращения + LLM + список участников),
сроки (детерминированный резолвер), проверка цитат (verifier), приоритет, направление, уверенность."""
import datetime as dt
import re

from .llm.speaker_names import map_speakers
from .llm.deadline_resolver import resolve
from .llm import verifier

CAT_MAP = {"производство": "Производство", "финансы": "Финансы", "закупки и поставки": "Закупки",
           "договоры и юридические вопросы": "Юридическое", "охрана труда и безопасность": "Охрана труда",
           "персонал и обучение": "Кадры", "отчётность": "Отчётность", "другое": "Другое"}
ORG_UNIT = re.compile(r"департамент|отдел|служб|управлени|бөлім|болим|комисси|дирекц|юристы", re.I)
FOLD = str.maketrans({"қ": "к", "ғ": "г", "ү": "у", "ұ": "у", "ә": "а", "ө": "о", "і": "и", "ң": "н", "һ": "х", "ё": "е"})


def fold(s: str) -> str:
    return (s or "").lower().translate(FOLD)


def first_key(name: str) -> str:
    w = fold(name).split()
    return w[0][:4] if w else ""


def canon_name(name: str | None, known: list[str]) -> str | None:
    """Приводит имя к написанию из списка участников/говорящих по совпадению начала имени."""
    if not name:
        return name
    k = first_key(name)
    for n in known:
        if n and first_key(n) == k:
            return n
    return name


def priority(due: str | None, meeting_date: str, llm_urgency: str | None) -> str:
    if due:
        days = (dt.date.fromisoformat(due) - dt.date.fromisoformat(meeting_date)).days
        return "high" if days <= 3 else ("mid" if days <= 10 else "low")
    return {"высокая": "high", "средняя": "mid"}.get(llm_urgency or "", "low")


def build(extraction: dict, transcript: str, meeting_date: str, participants_hint: list[str], voice_names: dict | None = None):
    """Возвращает (speaker_names{label: {name, role, source, confidence}}, tasks[list], summary{})."""
    heur, _ = map_speakers(transcript)
    llm_p = {}
    for p in extraction.get("participants", []) or []:
        nm = p.get("name")
        if str(nm).strip().lower() in ("null", "none", "", "-", "—", "неизвестно"):
            nm = None
        llm_p[p.get("speaker_label")] = {**p, "name": nm}
    labels = sorted(set(heur) | set(llm_p) | set(voice_names or {}))
    speakers = {}
    for lab in labels:
        h, l = heur.get(lab, {}), llm_p.get(lab, {})
        vn = (voice_names or {}).get(lab)
        if vn:  # узнан по голосовому профилю — самый надёжный источник
            name, src, conf = vn["name"], "голос", float(vn["sim"])
        elif h.get("name"):
            name, src, conf = h["name"], "обращения", min(0.99, 0.6 + 0.1 * (h.get("score") or 0))
        elif l.get("name"):
            name, src, conf = l["name"], "LLM", float(l.get("confidence") or 0.6)
        else:
            name, src, conf = None, None, 0.0
        name = canon_name(name, participants_hint)
        speakers[lab] = {"name": name, "role": l.get("role"), "source": src, "confidence": round(conf, 2)}
    # одно имя не может принадлежать двум меткам — оставляем более уверенную
    seen = {}
    for lab, s in sorted(speakers.items(), key=lambda kv: -kv[1]["confidence"]):
        if s["name"]:
            k = first_key(s["name"])
            if k in seen:
                s["name"], s["source"] = None, None
            else:
                seen[k] = lab
    known = [s["name"] for s in speakers.values() if s["name"]] + list(participants_hint)

    ext = {**extraction, "participants": [{"speaker_label": lab, "name": s["name"]} for lab, s in speakers.items()]}
    items, _ = verifier.verify(ext, transcript, use_llm=False, meeting_date=meeting_date)
    tasks = []
    for it in items:
        if it.get("_status") == "dropped":
            continue
        raw = (it.get("deadline_raw") or "не указан").strip()
        r = resolve(raw, meeting_date)
        due = r["iso"]
        owner = (it.get("assignee") or "").strip()
        lab_ref = re.fullmatch(r"\[?(SPEAKER_\d+)\]?", owner)
        if lab_ref or not owner:  # LLM вернула метку вместо имени — подставляем имя говорящего
            lab = lab_ref.group(1) if lab_ref else it.get("assignee_speaker_label")
            owner = speakers.get(lab, {}).get("name") or owner or "Не определён"
        owner = canon_name(owner, known)
        if ORG_UNIT.search(owner):
            kind = "department"
        elif any(first_key(owner) == first_key(n) for n in known if n):
            kind = "person"
        else:
            kind = "absent"
        issuer_lab = it.get("source_speaker_label")
        issued_by = speakers.get(issuer_lab, {}).get("name") or it.get("issued_by") or issuer_lab
        flags = it.get("_flags", [])
        status = it.get("_status")
        conf = 95 if status == "ok" else 90 if status == "auto_fixed" else 72
        if any(f.startswith("quote_fuzzy") for f in flags):
            conf -= 5
        tasks.append({
            "title": (it.get("description") or "").strip().rstrip("."),
            "owner": owner, "owner_kind": kind,
            "co_owners": [canon_name(speakers.get(c, {}).get("name") or c, known) for c in (it.get("co_assignees") or []) if c],
            "issued_by": issued_by, "due": due, "deadline_raw": raw, "deadline_kind": r["kind"],
            "priority": priority(due, meeting_date, it.get("urgency")),
            "direction": CAT_MAP.get(it.get("category") or "", "Другое"),
            "conf": max(50, conf), "quote": it.get("source_quote") or "", "seg_idx": it.get("_turn"),
            "flags": flags, "review": status == "needs_review",
        })
    s = extraction.get("summary") or {}
    summary = {"short": s.get("short_summary") or "", "decisions": s.get("key_decisions") or [],
               "topics_raw": s.get("topics") or [],
               "numbers": [{"value": n.get("value", ""), "meaning": n.get("meaning", "")} for n in s.get("key_numbers") or []]}
    return speakers, tasks, summary


def topic_times(topics: list[str], segments: list[dict]) -> list[dict]:
    """Привязывает темы повестки к первой реплике с наибольшим совпадением основ слов."""
    out = []
    last = 0
    for tp in topics:
        stems = {w[:5] for w in re.findall(r"\w+", fold(tp)) if len(w) > 3}
        best, best_i = 0, None
        for i, sg in enumerate(segments):
            ws = {w[:5] for w in re.findall(r"\w+", fold(sg["text"])) if len(w) > 3}
            sc = len(stems & ws) - (0.5 if i < last else 0)
            if sc > best:
                best, best_i = sc, i
        t = segments[best_i]["start"] if best_i is not None else 0
        if best_i is not None:
            last = best_i
        out.append({"label": tp, "start": t})
    return out
