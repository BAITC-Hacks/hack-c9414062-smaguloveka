"""Детерминированная привязка SPEAKER_xx -> имя по обращениям (без LLM, stdlib).
Правило «обращение -> ответ»: если в реплике говорящего A есть обращение по имени N («Тимур Болатович, что у вас?»,
«Жақсы, Марат Серікұлы, ...», «Начнём с Ботагоз Нурлановны»), то СЛЕДУЮЩУЮ реплику, скорее всего, произносит N
(если это не сам A). Голоса суммируются по всему совещанию, затем — взаимно-однозначное назначение с максимальным
суммарным весом (перебор перестановок, спикеров мало). Обращение в конце реплики или с вопросом весит больше."""
import re, itertools, json, sys

CAP = r"[А-ЯЁӘҒҚҢӨҰҮҺІ][а-яёәғқңөұүһі]+"
PATRON = r"(?:ович|евич|ич|овна|евна|ична|инична|вна|улы|ұлы|уулу|қызы|кызы)"
PATRON_ANY = PATRON[:-1] + r"|овны|евны|ичны|овне|евне|овну|евну|овича|евича|овичу|евичу)"
NOM_PATRON = r"(?:ович|евич|овна|евна|ична|улы|ұлы|қызы|кызы)"
try:
    from ..text import FIRST_NAMES as _FN
    FIRST_NOM = set(_FN)
except Exception:  # автономный запуск
    FIRST_NOM = set()
STOP = set("""так хорошо понятно коллеги смотрите спасибо отлично согласен логично значит итого первое второе третье
четвертое пятое да нет ну вот тогда сделаю принято понял поняла жарайды жақсы түсінікті болды рахмет әріптестер
салем сәлем иә жоқ мен біз сіз ал енді и а но если по привет давайте может сделаем слушайте кстати конечно
вообще наверное возможно кажется во-первых во-вторых кроме""".split())


def norm_name(n):
    parts = n.split()
    out = []
    for p in parts:
        p = re.sub(r"(ов|ев)ны$|(ов|ев)не$|(ов|ев)ну$", lambda m: (m[1] or m[2] or m[3]) + "на", p)
        p = re.sub(r"(ович|евич)(а|у|ем)$", r"\1", p)
        out.append(p)
    if len(out) == 2 and out[1].endswith("на") and out[0].endswith("ы"):  # «Ботагоз Нурлановны» -> first name not declined
        pass
    return " ".join(out)


def vocatives(text, full_lower=""):
    """Кандидаты-обращения в реплике: [(name, position_weight)].
    Однословное «имя» отбрасывается, если это слово встречается в транскрипте со строчной буквы (нарицательное)."""
    res = []
    sents = re.split(r"(?<=[.!?])\s+", text)
    for si, s in enumerate(sents):
        is_last = si == len(sents) - 1
        has_q = s.strip().endswith("?")
        # «Имя Отчество,» / «Имя,» в начале предложения или после вводного слова с запятой
        for m in re.finditer(r"(?:^|(?<=[,—–-])\s*)(" + CAP + r"(?:\s+" + CAP + PATRON + r")?)\s*(?=,)", s):
            name = m[1]
            if name.split()[0].lower() in STOP:
                continue
            stem = name.lower()[:max(4, len(name) - 2)]
            if " " not in name and re.search(r"(?<![\w])" + stem, full_lower):
                continue  # нарицательное слово («Подрядчик, который…»)
            w = 1.0 + (0.5 if is_last else 0) + (0.5 if has_q else 0) + (0.25 if " " in name else 0)
            res.append((norm_name(name), w))
        # «Начнём с Ботагоз Нурлановны», «слово ... Имени Отчеству»
        for m in re.finditer(r"(?:[Нн]ачн[её]м с|слово)\s+(" + CAP + r"\s+" + CAP + PATRON_ANY + r")", s):
            res.append((norm_name(m[1]), 2.0))
    if "," not in text:  # текст ASR без пунктуации: имя-отчество (им. падеж) или имя из словаря в любом месте реплики
        words = text.split()
        for m in re.finditer(r"(" + CAP + r")(?:\s+(" + CAP + NOM_PATRON + r"))?\b", text):
            name = m[1] + (" " + m[2] if m[2] else "")
            if not m[2] and m[1].lower() not in FIRST_NOM:
                continue
            before = text[:m.start()].split()
            if before and re.match(r"ответствен|отв$|с$|со$|у$|к$|от$|для$|по$", before[-1].lower()):
                continue
            pos = len(before)
            w = 0.8 + (0.5 if pos >= len(words) - 9 else 0) + (0.3 if m[2] else 0)
            res.append((norm_name(name), w))
    return res


def full_lower_cs(t):
    return t


def map_speakers(transcript):
    # строчные вхождения слов (регистр важен): берём только слова, написанные со строчной буквы
    lower_words = " ".join(w for w in re.findall(r"[\w-]+", transcript) if w[:1].islower())
    turns = [(m[1], m[2]) for m in re.finditer(r"^\[(SPEAKER_\d+)\]\s*(.*)$", transcript, re.M)]
    labels = sorted({t[0] for t in turns})
    votes = {}
    for i, (spk, text) in enumerate(turns):
        voc = vocatives(text, lower_words)
        # следующая реплика (вес 1) и через одну (вес 0.5): адресат иногда отвечает не сразу
        for off, decay in ((1, 1.0), (2, 0.5)):
            if i + off >= len(turns):
                continue
            nxt = turns[i + off][0]
            if nxt == spk:
                continue
            for name, w in voc:
                votes[(nxt, name)] = votes.get((nxt, name), 0) + w * decay
    # объединяем «Имя» и «Имя Отчество» одного человека
    names = sorted({n for _, n in votes}, key=len, reverse=True)
    canon = {}
    for n in names:
        full = next((f for f in names if f != n and f.split()[0] == n.split()[0] and len(f) > len(n)), None)
        canon[n] = full or n
    v2 = {}
    for (lab, n), w in votes.items():
        v2[(lab, canon[n])] = v2.get((lab, canon[n]), 0) + w
    # взаимно-однозначное назначение с максимальной суммой голосов (DFS; только пары с голосом > 0)
    opts = {lab: [n for (l, n) in v2 if l == lab] for lab in labels}
    best, best_score = {}, -1.0

    def dfs(i, used, cur, score):
        nonlocal best, best_score
        if i == len(labels):
            if score > best_score:
                best_score, best = score, dict(cur)
            return
        lab = labels[i]
        for n in opts[lab]:
            if n not in used:
                cur[lab] = n; dfs(i + 1, used | {n}, cur, score + v2[(lab, n)]); del cur[lab]
        dfs(i + 1, used, cur, score)  # метка без имени

    dfs(0, frozenset(), {}, 0.0)
    return {lab: {"name": best.get(lab), "score": round(v2.get((lab, best.get(lab)), 0), 2)} for lab in labels}, v2


if __name__ == "__main__":
    for key in sys.argv[1:] or ["m1", "m2", "kk", "shala"]:
        tr = open(f"inputs/{key}_transcript.txt", encoding="utf-8").read()
        gold = json.load(open(f"inputs/{key}_speakers_gold.json", encoding="utf-8"))
        res, votes = map_speakers(tr)
        ok = 0
        for lab, r in res.items():
            g = gold[lab]["name"]
            spoken = g.split()[0][:4].lower() in tr.lower()
            good = (r["name"] or "").split()[:1] == g.split()[:1] if spoken else r["name"] is None
            ok += good
            print(f"  {key} {lab}: {r['name']!s:25} score={r['score']:<4} gold={g if spoken else None} {'OK' if good else 'ERR'}")
        print(f"{key}: {ok}/{len(res)}")
