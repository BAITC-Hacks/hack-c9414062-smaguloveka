"""Проверка качества ASR (GigaAM-Multilingual) на синтетических KZ / шала-казахских совещаниях.

Для каждой реплики из samples/*.turns.json (точные границы известны из генерации) запускает
app.pipeline.asr.transcribe и считает CER/WER против сценария, по языкам (KZ / RU / RU+KZ).
Отдельно — распознаются ли казахские буквы ә ғ қ ң ө ұ ү һ і и типичные ошибки.

Запуск (из корня проекта): .venv/bin/python scripts/asr_check.py [samples/kk_meeting samples/shala_meeting]
Результат: samples/asr_report.md и samples/asr_results.json
"""
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KZ_LETTERS = "әғқңөұүһі"
# во что «естественно» превращается казахская буква, если модель её не знает
KZ_FOLD = {"ә": "а", "ғ": "г", "қ": "к", "ң": "н", "ө": "о", "ұ": "у", "ү": "у", "һ": "х", "і": "и"}


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def align(ref, hyp):
    """Левенштейн с обратным проходом. Возвращает (distance, ops[(op, r, h)]), op in = S D I."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        ri = ref[i - 1]
        row, prev = d[i], d[i - 1]
        for j in range(1, m + 1):
            c = 0 if ri == hyp[j - 1] else 1
            row[j] = min(prev[j - 1] + c, prev[j] + 1, row[j - 1] + 1)
    ops, i, j = [], n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1):
            ops.append(("=" if ref[i - 1] == hyp[j - 1] else "S", ref[i - 1], hyp[j - 1])); i -= 1; j -= 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            ops.append(("D", ref[i - 1], None)); i -= 1
        else:
            ops.append(("I", None, hyp[j - 1])); j -= 1
    return d[n][m], ops[::-1]


def main():
    stems = sys.argv[1:] or ["samples/kk_meeting", "samples/shala_meeting"]
    from app.pipeline.audio import load_16k
    from app.pipeline.asr import transcribe, model

    t0 = time.time()
    model()
    load_s = time.time() - t0

    agg = defaultdict(lambda: Counter())       # lang -> counters (ce, cn, we, wn)
    per_meeting = defaultdict(lambda: Counter())
    letter = defaultdict(Counter)               # kz letter -> {'ok', 'sub:<x>', 'del'}
    kz_in_hyp = Counter()
    word_subs = Counter()
    rows, results = [], []
    asr_time = audio_time = 0.0

    for stem in stems:
        meta = json.loads(Path(ROOT / f"{stem}.turns.json").read_text(encoding="utf-8"))
        audio = load_16k(str(ROOT / f"{stem}.wav"))
        for tr in meta["turns"]:
            t1 = time.time()
            hyp = transcribe(audio, tr["start"], tr["end"])
            asr_time += time.time() - t1
            audio_time += tr["end"] - tr["start"]
            r, h = norm(tr["text"]), norm(hyp)
            ce, cops = align(list(r.replace(" ", "")), list(h.replace(" ", "")))
            cn = len(r.replace(" ", ""))
            we, wops = align(r.split(), h.split())
            wn = len(r.split())
            for key in (tr["lang"], "ALL"):
                agg[key].update(ce=ce, cn=cn, we=we, wn=wn)
            per_meeting[meta["meeting"]].update(ce=ce, cn=cn, we=we, wn=wn)
            for op, a, b in cops:
                if a and a in KZ_LETTERS:
                    letter[a]["ref"] += 1
                    if op == "=":
                        letter[a]["ok"] += 1
                    elif op == "S":
                        letter[a][f"→{b}"] += 1
                    else:
                        letter[a]["→∅"] += 1
            for ch in h:
                if ch in KZ_LETTERS:
                    kz_in_hyp[ch] += 1
            for op, a, b in wops:
                if op == "S":
                    word_subs[(a, b)] += 1
                elif op == "D":
                    word_subs[(a, "∅")] += 1
                elif op == "I":
                    word_subs[("∅", b)] += 1
            rows.append((meta["meeting"], tr["idx"], tr["name"], tr["lang"], ce / max(cn, 1), we / max(wn, 1), tr["text"], hyp))
            results.append(dict(meeting=meta["meeting"], idx=tr["idx"], speaker=tr["name"], lang=tr["lang"],
                                start=tr["start"], end=tr["end"], ref=tr["text"], hyp=hyp,
                                cer=round(ce / max(cn, 1), 4), wer=round(we / max(wn, 1), 4)))
            print(f"[{meta['meeting']} #{tr['idx']:02d} {tr['lang']:5s}] CER {ce / max(cn, 1):.3f}  {hyp}", flush=True)

    def pct(c, e, n):
        return f"{100 * c[e] / max(c[n], 1):.1f}%"

    md = ["# Проверка ASR на казахском и шала-казахском (синтетические записи)", "",
          "Модель: GigaAM-Multilingual large CTC, int8 ONNX, CPU (`app/pipeline/asr.py`). "
          "Нарезка по эталонным границам реплик из `samples/*.turns.json` (диаризация не участвует). "
          "Нормализация: нижний регистр, ё→е, без пунктуации. CER считается без пробелов.", "",
          f"Загрузка модели: {load_s:.1f} с; распознавание {audio_time:.0f} с аудио за {asr_time:.1f} с "
          f"(RTF {asr_time / max(audio_time, 1e-9):.3f}).", "",
          "## Итог по языкам", "", "| Язык реплик | Символов | CER | Слов | WER |", "|---|---|---|---|---|"]
    for k in ["KZ", "RU+KZ", "RU", "ALL"]:
        if k in agg:
            c = agg[k]
            md.append(f"| {k} | {c['cn']} | {pct(c, 'ce', 'cn')} | {c['wn']} | {pct(c, 'we', 'wn')} |")
    md += ["", "| Запись | CER | WER |", "|---|---|---|"]
    for k, c in per_meeting.items():
        md.append(f"| {k} | {pct(c, 'ce', 'cn')} | {pct(c, 'we', 'wn')} |")

    md += ["", "## Казахские буквы", "",
           "Сколько раз буква встречалась в эталоне, сколько распознано верно и во что превращалась при ошибке.", "",
           "| Буква | В эталоне | Верно | Доля | Замены |", "|---|---|---|---|---|"]
    tot_ref = tot_ok = 0
    for ch in KZ_LETTERS:
        c = letter.get(ch, Counter())
        n, ok = c["ref"], c["ok"]
        tot_ref += n; tot_ok += ok
        subs = ", ".join(f"{k} ×{v}" for k, v in c.most_common() if k.startswith("→"))
        md.append(f"| {ch} | {n} | {ok} | {100 * ok / n:.0f}% | {subs or '—'} |" if n else f"| {ch} | 0 | — | — | — |")
    md += ["", f"Всего казахских букв: {tot_ref}, распознано верно {tot_ok} ({100 * tot_ok / max(tot_ref, 1):.1f}%). "
           f"В гипотезах встретились: {', '.join(f'{k}×{v}' for k, v in kz_in_hyp.most_common()) or 'нет'}.", "",
           "## Типичные ошибки (слова)", "", "| Эталон | Распознано | Раз |", "|---|---|---|"]
    for (a, b), v in word_subs.most_common(25):
        md.append(f"| {a} | {b} | {v} |")
    md += ["", "## По репликам", "", "| Запись | # | Говорящий | Язык | CER | WER | Эталон | Распознано |",
           "|---|---|---|---|---|---|---|---|"]
    for mt, i, nm, lg, cer, wer, ref, hyp in rows:
        md.append(f"| {mt} | {i} | {nm} | {lg} | {100 * cer:.1f}% | {100 * wer:.1f}% | {ref} | {hyp} |")

    (ROOT / "samples/asr_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (ROOT / "samples/asr_results.json").write_text(json.dumps(
        {"by_lang": {k: dict(v) for k, v in agg.items()}, "turns": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(md[:30]))


if __name__ == "__main__":
    main()
