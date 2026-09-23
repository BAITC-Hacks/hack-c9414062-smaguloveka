"""Оценка извлечённых поручений против эталона (gold).

Примеры:
  python scripts/eval.py --meeting-json out.json --gold eval/gold.json --meeting 1
  python scripts/eval.py --api http://localhost:8000 --id 3 --gold samples/shala_meeting.gold.json
  python scripts/eval.py --meeting-json out.json --gold eval/gold.json --meeting 2 --json-out report.json

meeting-json — объект из GET /api/meetings/{id} (см. CONTRACT.md), либо {"tasks":[...]}, либо просто список задач.
Метрики:
  * recall / precision / F1 по поручениям. Сопоставление предсказанных задач с эталоном — один-к-одному (венгерский алгоритм)
    по текстовому сходству (формулировка и цитата, токены усечены до 5 символов + символьные 3-граммы),
    ответственный и срок в сходство НЕ входят (иначе метрики по ним завышаются).
  * точность ответственного на сопоставленных (нормализация ё→е, казахские буквы → русские аналоги,
    сравнение имени и основы отчества; поддержка департаментов; соисполнитель = частичное совпадение 0.5);
  * точность срока (due == deadline_iso или оба пустые) и дополнительно с допуском ±2 дня.
Только stdlib.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

KZ_FOLD = str.maketrans({"ә": "а", "ғ": "г", "қ": "к", "ң": "н", "ө": "о", "ұ": "у", "ү": "у", "һ": "х", "і": "и",
                         "ё": "е", "Ә": "а", "Ғ": "г", "Қ": "к", "Ң": "н", "Ө": "о", "Ұ": "у", "Ү": "у", "Һ": "х",
                         "І": "и", "Ё": "е"})
STOP = {"срок", "ответ", "отвеч", "котор", "также", "этого", "после", "нужно", "необх", "until", "дейин", "бойын",
        "задач", "поруч", "совещ", "котор", "будет", "всех", "всем", "перед"}
DEPT_WORDS = {"департамент", "департамента", "департаменту", "отдел", "отдела", "отделу", "служба", "службы", "службе",
              "управление", "управления", "управлению", "дирекция", "дирекции", "департаменти", "болим", "блок"}
DEPT_ALIASES = {"юрист": "юрид", "юрдеп": "юрид", "юрсл": "юрид", "кадро": "персо", "hr": "персо", "бухга": "финан",
                "финдеп": "финан", "закуп": "закуп", "сатып": "закуп"}


def norm(s) -> str:
    s = str(s or "").translate(KZ_FOLD).lower()
    s = s.replace("\\", " ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def stems(s, n=5) -> list:
    return [w[:n] for w in norm(s).split() if len(w) >= 3 and w[:n] not in STOP and not w.isdigit()] + \
           [w for w in norm(s).split() if w.isdigit()]


def f1_tokens(a, b) -> float:
    sa, sb = set(stems(a)), set(stems(b))
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    if not inter:
        return 0.0
    p, r = inter / len(sb), inter / len(sa)
    return 2 * p * r / (p + r)


def dice3(a, b) -> float:
    def grams(s):
        s = f" {norm(s)} "
        return {s[i:i + 3] for i in range(len(s) - 2)}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def containment(a, b) -> float:
    """Доля основ короткой формулировки a (≥3 основ), встречающихся в b: краткий заголовок vs длинный эталон."""
    sa, sb = set(stems(a)), set(stems(b))
    if len(sa) < 3 or not sb:
        return 0.0
    return len(sa & sb) / len(sa)


def text_sim(a, b) -> float:
    if not norm(a) or not norm(b):
        return 0.0
    return max(f1_tokens(a, b), dice3(a, b), 0.8 * containment(a, b))


def pair_sim(p: dict, g: dict) -> float:
    title, quote = p.get("title") or "", p.get("quote") or ""
    cands = [text_sim(title, g.get("description")), text_sim(title, g.get("description_alt"))]
    if quote and g.get("source_quote"):
        cands.append(text_sim(quote, g["source_quote"]))
        cands.append(0.9 * text_sim(quote, g.get("description")))
    if title and g.get("source_quote"):
        cands.append(0.9 * text_sim(title, g["source_quote"]))
    return max(cands)


# ---------- ответственные ----------
def is_dept(s) -> bool:
    w = norm(s).split()
    return any(x in DEPT_WORDS or x.startswith(("департамент", "юрист", "юрдеп")) for x in w)


def dept_key(s) -> set:
    out = set()
    for w in norm(s).split():
        if w in DEPT_WORDS or w.startswith("департамент"):
            continue
        k = w[:5]
        out.add(DEPT_ALIASES.get(k, DEPT_ALIASES.get(w[:6], k))[:4])
    return {k for k in out if len(k) >= 2}


def _near(a: str, b: str) -> bool:
    """Одно и то же слово с учётом ошибки ASR в одной букве («Батагоз» ~ «Ботагоз»)."""
    import difflib
    return len(a) >= 4 and len(b) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def person_match(gold_name, pred_name) -> bool:
    g, p = norm(gold_name).split(), norm(pred_name).split()
    if g and p and len(g) == len(p) and all(_near(a, b) for a, b in zip(g, p)):
        return True
    if not g or not p:
        return False
    pst = [w[:5] for w in p]
    first = g[0][:5] if len(g[0]) >= 5 else g[0]
    if not any(x.startswith(first) or (len(x) >= 4 and first.startswith(x)) for x in pst):
        # возможно, «Отчество Имя» или только отчество
        if len(g) > 1 and any(x[:5] == g[1][:5] for x in pst) and len(p) == 1:
            return True
        return False
    if len(g) > 1 and len(p) > 1:
        pat = g[1][:5]
        return any(x[:5] == pat or (len(x) >= 5 and x[:4] == pat[:4]) or (len(x) == 1 and pat.startswith(x))
                   for x in p[1:] + p[:1])
    return True


def owner_score(gold: dict, pred: dict) -> float:
    ga, po = gold.get("assignee") or "", pred.get("owner") or ""
    if not norm(ga):
        return 1.0 if not norm(po) else 0.0
    if not norm(po):
        return 0.0
    if is_dept(ga) or gold.get("assignee_kind") == "department":
        return 1.0 if dept_key(ga) & dept_key(po) else 0.0
    if person_match(ga, po):
        return 1.0
    cos = gold.get("co_assignees") or []
    if any(person_match(c, po) for c in cos):
        return 0.5
    if any(person_match(ga, c) for c in (pred.get("co_owners") or [])):
        return 0.5
    return 0.0


# ---------- сроки ----------
def iso(s):
    s = str(s or "").strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def deadline_scores(gold: dict, pred: dict):
    gi, pi = iso(gold.get("deadline_iso")), iso(pred.get("due"))
    if gi is None and pi is None:
        return 1, 1
    if gi is None or pi is None:
        return 0, 0
    return int(gi == pi), int(abs((gi - pi).days) <= 2)


# ---------- загрузка ----------
def load_pred(args) -> tuple:
    if args.api:
        url = f"{args.api.rstrip('/')}/api/meetings/{args.id}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                obj = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Не удалось получить {url}: {e}")
    else:
        obj = json.loads(Path(args.meeting_json).read_text(encoding="utf-8"))
    if isinstance(obj, list):
        tasks, meta = obj, {}
    else:
        tasks, meta = obj.get("tasks") or [], obj
        if args.id is not None and meta.get("meetings") and "segments" not in meta:  # /api/state
            tasks = [t for t in tasks if str(t.get("meeting_id")) == str(args.id)]
    out = []
    for i, t in enumerate(tasks):
        out.append(dict(
            num=t.get("num") or t.get("id") or str(i + 1),
            title=t.get("title") or t.get("description") or t.get("text") or "",
            owner=t.get("owner") or t.get("assignee") or "",
            co_owners=t.get("co_owners") or t.get("co_assignees") or [],
            due=t.get("due") or t.get("deadline_iso") or t.get("deadline") or None,
            deadline_raw=t.get("deadline_raw") or "",
            quote=t.get("quote") or t.get("source_quote") or "",
        ))
    return out, meta


def load_gold(path, meeting):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = obj.get("tasks", obj) if isinstance(obj, dict) else obj
    if meeting is not None:
        tasks = [t for t in tasks if str(t.get("meeting")) == str(meeting)]
    return tasks


# ---------- основной расчёт ----------
def hungarian(n: int, m: int, W: dict) -> list:
    """Максимальное по весу паросочетание (венгерский алгоритм, O(N^3)); пары без веса не назначаются."""
    N = max(n, m)
    if N == 0 or not W:
        return []
    big = max(W.values()) + 1.0
    cost = [[big - W.get((i, j), 0.0) if (i, j) in W else big for j in range(N)] for i in range(N)]
    INF = float("inf")
    u, v, p, way = [0.0] * (N + 1), [0.0] * (N + 1), [0] * (N + 1), [0] * (N + 1)
    for i in range(1, N + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (N + 1), [False] * (N + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, N + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(N + 1):
                if used[j]:
                    u[p[j]] += delta; v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
            if j0 == 0:
                break
    return [(p[j] - 1, j - 1) for j in range(1, N + 1) if p[j] and (p[j] - 1, j - 1) in W]


def evaluate(gold: list, pred: list, threshold: float) -> dict:
    pairs = []
    for gi, g in enumerate(gold):
        for pi, p in enumerate(pred):
            s = pair_sim(p, g)
            if s >= threshold:
                pairs.append((s, owner_score(g, p), gi, pi))
    # оптимальное назначение один-к-одному (максимум суммарного сходства), при равенстве — у кого верный ответственный
    W = {(gi, pi): s + 1e-3 * o for s, o, gi, pi in pairs}
    sims = {(gi, pi): s for s, o, gi, pi in pairs}
    used_g, used_p, matches = set(), set(), []
    for gi, pi in hungarian(len(gold), len(pred), W):
        used_g.add(gi); used_p.add(pi)
        s = sims[(gi, pi)]
        g, p = gold[gi], pred[pi]
        ex, tol = deadline_scores(g, p)
        matches.append(dict(gold=g, pred=p, sim=round(s, 3), owner=owner_score(g, p), due=ex, due_tol=tol))
    matches.sort(key=lambda m: str(m["gold"].get("id")))
    tp = len(matches)
    fn = [g for i, g in enumerate(gold) if i not in used_g]
    fp = [p for i, p in enumerate(pred) if i not in used_p]
    # «дубли»: лишние предсказания, похожие на уже найденную задачу
    dups = [p for p in fp if any(pair_sim(p, m["gold"]) >= threshold for m in matches)]
    rec = tp / len(gold) if gold else 0.0
    prec = tp / len(pred) if pred else 0.0
    f1 = 2 * rec * prec / (rec + prec) if rec + prec else 0.0
    own = sum(m["owner"] for m in matches)
    due = sum(m["due"] for m in matches)
    due_tol = sum(m["due_tol"] for m in matches)
    by_diff = {}
    for g in gold:
        d = g.get("extraction_difficulty")
        if d:
            by_diff.setdefault(d, [0, 0])[1] += 1
    for m in matches:
        d = m["gold"].get("extraction_difficulty")
        if d:
            by_diff[d][0] += 1
    return dict(n_gold=len(gold), n_pred=len(pred), tp=tp, recall=rec, precision=prec, f1=f1,
                owner_acc=own / tp if tp else 0.0, owner_sum=own, due_acc=due / tp if tp else 0.0, due_sum=due,
                due_tol_acc=due_tol / tp if tp else 0.0, due_tol_sum=due_tol, matches=matches, fn=fn, fp=fp,
                dups=dups, by_difficulty=by_diff,
                # сквозные метрики: «поручение найдено И ответственный/срок верны» от числа эталонных
                owner_e2e=own / len(gold) if gold else 0.0, due_e2e=due / len(gold) if gold else 0.0)


def pct(x):
    return f"{100 * x:.1f}%"


def cut(s, n=70):
    s = str(s or "").replace("|", "/").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def render(r: dict, title: str) -> str:
    L = [f"## {title}", "", "| Метрика | Значение |", "|---|---|",
         f"| Эталонных поручений | {r['n_gold']} |",
         f"| Предсказано системой | {r['n_pred']} |",
         f"| Найдено (TP) | {r['tp']} |",
         f"| Recall | {pct(r['recall'])} |",
         f"| Precision | {pct(r['precision'])} |",
         f"| F1 | {pct(r['f1'])} |",
         f"| Ответственный верно (из найденных) | {r['owner_sum']:g}/{r['tp']} = {pct(r['owner_acc'])} |",
         f"| Срок верно, точная дата (из найденных) | {r['due_sum']}/{r['tp']} = {pct(r['due_acc'])} |",
         f"| Срок верно, ±2 дня (из найденных) | {r['due_tol_sum']}/{r['tp']} = {pct(r['due_tol_acc'])} |",
         f"| Сквозная: найдено + ответственный верно | {pct(r['owner_e2e'])} |",
         f"| Сквозная: найдено + срок верно | {pct(r['due_e2e'])} |",
         f"| Дубли среди лишних | {len(r['dups'])} |"]
    for d, (k, n) in sorted(r["by_difficulty"].items()):
        L.append(f"| Recall ({d}) | {k}/{n} = {pct(k / n if n else 0)} |")
    L += ["", "### Сопоставленные поручения", "",
          "| Эталон | Система | Сходство | Ответственный: эталон → система | Срок: эталон → система |",
          "|---|---|---|---|---|"]
    for m in r["matches"]:
        g, p = m["gold"], m["pred"]
        om = {1.0: "✓", 0.5: "½"}.get(m["owner"], "✗")
        dm = "✓" if m["due"] else ("≈" if m["due_tol"] else "✗")
        L.append(f"| {g.get('id')} {cut(g.get('description'), 50)} | {p['num']} {cut(p['title'], 50)} | {m['sim']:.2f} | "
                 f"{om} {g.get('assignee') or '—'} → {p['owner'] or '—'} | "
                 f"{dm} {g.get('deadline_iso') or '—'} → {p['due'] or '—'} |")
    mism = [m for m in r["matches"] if m["owner"] < 1 or not m["due"]]
    if mism:
        L += ["", "### Расхождения по найденным", ""]
        for m in mism:
            g, p = m["gold"], m["pred"]
            if m["owner"] < 1:
                L.append(f"- {g.get('id')}: ответственный «{g.get('assignee')}» ≠ «{p['owner'] or '—'}» (балл {m['owner']:g})")
            if not m["due"]:
                L.append(f"- {g.get('id')}: срок {g.get('deadline_iso') or 'нет'} ({g.get('deadline_raw') or '—'}) ≠ "
                         f"{p['due'] or 'нет'} ({p['deadline_raw'] or '—'})")
    if r["fn"]:
        L += ["", "### Пропущено системой (FN)", ""]
        for g in r["fn"]:
            L.append(f"- {g.get('id')}: {cut(g.get('description'), 110)} — {g.get('assignee') or '—'}, {g.get('deadline_iso') or '—'}")
    if r["fp"]:
        L += ["", "### Лишние предсказания (FP)", ""]
        for p in r["fp"]:
            tag = " (дубль)" if p in r["dups"] else ""
            L.append(f"- {p['num']}: {cut(p['title'], 110)} — {p['owner'] or '—'}, {p['due'] or '—'}{tag}")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Оценка поручений против эталона")
    ap.add_argument("--meeting-json", help="JSON из GET /api/meetings/{id} (или {tasks:[...]}, или список задач)")
    ap.add_argument("--api", help="например http://localhost:8000 — забрать совещание с сервера")
    ap.add_argument("--id", help="id совещания на сервере (для --api)")
    ap.add_argument("--gold", required=True, help="eval/gold.json или samples/*.gold.json")
    ap.add_argument("--meeting", help="номер совещания в gold (поле tasks[].meeting), напр. 1")
    ap.add_argument("--threshold", type=float, default=0.45, help="порог текстового сходства для сопоставления")
    ap.add_argument("--json-out", help="сохранить метрики в JSON")
    a = ap.parse_args()
    if not a.meeting_json and not (a.api and a.id):
        ap.error("нужен --meeting-json или --api + --id")
    gold = load_gold(a.gold, a.meeting)
    if not gold:
        sys.exit(f"В {a.gold} нет эталонных задач (meeting={a.meeting})")
    pred, meta = load_pred(a)
    r = evaluate(gold, pred, a.threshold)
    title = f"Оценка поручений: {meta.get('title') or a.meeting_json or a.id} vs {Path(a.gold).name}" + \
            (f" (совещание {a.meeting})" if a.meeting else "")
    print(render(r, title))
    if a.json_out:
        slim = {k: v for k, v in r.items() if k not in ("matches", "fn", "fp", "dups")}
        slim["matches"] = [dict(gold=m["gold"].get("id"), pred=m["pred"]["num"], sim=m["sim"], owner=m["owner"],
                                due=m["due"], due_tol=m["due_tol"]) for m in r["matches"]]
        slim["fn"] = [g.get("id") for g in r["fn"]]
        slim["fp"] = [p["num"] for p in r["fp"]]
        Path(a.json_out).write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
