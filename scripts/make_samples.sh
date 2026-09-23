#!/usr/bin/env bash
# Генерация синтетических тестовых совещаний на казахском и шала-казахском (RU+KZ) языках.
# Полностью офлайн: системный TTS macOS (`say`, голоса Aru kk_KZ и Milena ru_RU) + ffmpeg.
#
# Результат:
#   samples/kk_meeting.{wav,mp3}      — совещание только на казахском, 3 говорящих
#   samples/shala_meeting.{wav,mp3}   — шала-казахский (RU+KZ внутри фраз), 4 говорящих
#   samples/*.turns.json              — точные границы реплик (эталон для ASR/диаризации)
#   samples/*.script.md               — сценарий (говорящий, роль, реплика)
#   samples/*.gold.json               — эталонные поручения (формат eval/gold.json, дата совещания 2026-09-23)
#   app/static/consent.mp3            — объявление о записи (KZ + RU)
#
# Разные говорящие = разные голоса + сдвиг высоты тона/тембра (asetrate+aresample+atempo, EQ, эхо),
# чтобы диаризатор мог их различить. Между репликами 0.4–0.7 с тишины.
#
# Запуск: bash scripts/make_samples.sh   (из корня проекта)
set -euo pipefail
cd "$(dirname "$0")/.."
command -v say >/dev/null || { echo "нужен macOS 'say'"; exit 1; }
command -v ffmpeg >/dev/null || { echo "нужен ffmpeg"; exit 1; }
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
mkdir -p samples app/static

"$PY" - <<'PYEOF'
import json, os, random, subprocess, tempfile
from pathlib import Path
import numpy as np
import soundfile as sf

SR = 16000
OUT = Path("samples")
TMP = Path(tempfile.mkdtemp(prefix="hatshy_tts_"))
MEETING_DATE = "2026-09-23"  # среда

# Голоса говорящих: базовый голос TTS, сдвиг тона (pitch), скорость, доп. фильтр («другой микрофон/комната»).
VOICES = {
    "kk_chair":  dict(voice="Aru",    pitch=0.80, rate=175, extra="lowpass=f=3800"),
    "kk_fin":    dict(voice="Aru",    pitch=1.00, rate=180, extra=""),
    "kk_prod":   dict(voice="Aru",    pitch=0.90, rate=170, extra="highpass=f=250,aecho=0.8:0.5:25:0.25"),
    "sh_chair":  dict(voice="Aru",    pitch=0.80, rate=175, extra="lowpass=f=3800"),
    "sh_fin":    dict(voice="Milena", pitch=1.00, rate=185, extra=""),
    "sh_prod":   dict(voice="Milena", pitch=0.80, rate=180, extra="highpass=f=200,aecho=0.8:0.5:25:0.25"),
    "sh_hr":     dict(voice="Aru",    pitch=1.12, rate=185, extra="highpass=f=150"),
}

MEETINGS = {
  "kk_meeting": {
    "title": "Жылыту маусымына дайындық және цифрландыру (синтетическая запись, KZ)",
    "speakers": {
      "kk_chair": ("Ерлан Маратұлы", "басқарма төрағасының орынбасары (председатель)"),
      "kk_fin":   ("Динара Серікқызы", "қаржы департаментінің директоры"),
      "kk_prod":  ("Ержан Болатұлы", "өндіріс департаментінің директоры"),
    },
    "turns": [
      ("kk_chair", "KZ", "Қайырлы күн, әріптестер. Бүгін жылыту маусымына дайындық және цифрландыру мәселесін қараймыз. Ержан Болатұлы, сізге сөз."),
      ("kk_prod",  "KZ", "Рақмет, Ерлан Маратұлы. Қазір он бір қазандықтың тоғызы дайын. Екі қазандықта жөндеу жұмыстары әлі аяқталған жоқ, құбырларды ауыстыру керек."),
      ("kk_chair", "KZ", "Түсінікті. Ержан Болатұлы, қалған екі қазандықтың жөндеуін бірінші қазанға дейін аяқтаңыз."),
      ("kk_prod",  "KZ", "Жақсы, орындаймыз. Бірақ бізге қосымша қаржы қажет."),
      ("kk_chair", "KZ", "Динара Серікқызы, қаржы жағдайы қандай?"),
      ("kk_fin",   "KZ", "Бюджетте қосымша үш жүз миллион теңге қарастырылған. Бірақ оны бөлу үшін негіздеме керек."),
      ("kk_chair", "KZ", "Онда Динара Серікқызы, қосымша қаржыға негіздемені жұма күнге дейін дайындаңыз."),
      ("kk_fin",   "KZ", "Жарайды, жұмаға дейін дайындаймын."),
      ("kk_chair", "KZ", "Енді цифрландыру. Барлық қазандықтарға датчиктер орнату жобасы дайын ба?"),
      ("kk_prod",  "KZ", "Иә, жоба дайын. Датчиктерді сатып алу үшін сатып алу департаментіне өтінім беру керек."),
      ("kk_chair", "KZ", "Ержан Болатұлы, датчиктерді сатып алуға өтінімді келесі дүйсенбіге дейін жіберіңіз. Ал Динара Серікқызы, айдың соңына дейін шығындар кестесін барлық басшыларға жіберіңіз."),
      ("kk_fin",   "KZ", "Түсіндім, айдың соңына дейін жіберемін."),
      ("kk_chair", "KZ", "Және соңғы тапсырма. Ержан Болатұлы, он бесінші қазанға дейін жылыту маусымына дайындық туралы толық есеп дайындаңыз. Барлығыңызға рақмет, кеңес аяқталды."),
    ],
    "gold": [
      dict(id="KK-T01", description="Завершить ремонт оставшихся двух котельных (замена труб)",
           description_alt="Қалған екі қазандықтың жөндеуін аяқтау",
           assignee="Ержан Болатұлы", deadline_raw="бірінші қазанға дейін", deadline_kind="absolute_date", deadline_iso="2026-10-01",
           source_quote="Ержан Болатұлы, қалған екі қазандықтың жөндеуін бірінші қазанға дейін аяқтаңыз."),
      dict(id="KK-T02", description="Подготовить обоснование на дополнительное финансирование (300 млн тенге)",
           description_alt="Қосымша қаржыға негіздеме дайындау",
           assignee="Динара Серікқызы", deadline_raw="жұма күнге дейін", deadline_kind="relative_week", deadline_iso="2026-09-25",
           source_quote="Онда Динара Серікқызы, қосымша қаржыға негіздемені жұма күнге дейін дайындаңыз."),
      dict(id="KK-T03", description="Направить заявку на закуп датчиков для котельных",
           description_alt="Датчиктерді сатып алуға өтінім жіберу",
           assignee="Ержан Болатұлы", deadline_raw="келесі дүйсенбіге дейін", deadline_kind="relative_week", deadline_iso="2026-09-28",
           source_quote="Ержан Болатұлы, датчиктерді сатып алуға өтінімді келесі дүйсенбіге дейін жіберіңіз."),
      dict(id="KK-T04", description="Разослать график (таблицу) расходов всем руководителям",
           description_alt="Шығындар кестесін барлық басшыларға жіберу",
           assignee="Динара Серікқызы", deadline_raw="айдың соңына дейін", deadline_kind="relative_month", deadline_iso="2026-09-30",
           source_quote="Ал Динара Серікқызы, айдың соңына дейін шығындар кестесін барлық басшыларға жіберіңіз."),
      dict(id="KK-T05", description="Подготовить полный отчёт о готовности к отопительному сезону",
           description_alt="Жылыту маусымына дайындық туралы толық есеп дайындау",
           assignee="Ержан Болатұлы", deadline_raw="он бесінші қазанға дейін", deadline_kind="absolute_date", deadline_iso="2026-10-15",
           source_quote="Ержан Болатұлы, он бесінші қазанға дейін жылыту маусымына дайындық туралы толық есеп дайындаңыз."),
    ],
  },
  "shala_meeting": {
    "title": "Планёрка: бюджет, график отпусков, охрана труда (синтетическая запись, RU+KZ шала)",
    "speakers": {
      "sh_chair": ("Бауыржан Асқарович", "председатель, заместитель генерального директора"),
      "sh_fin":   ("Динара Серікқызы", "директор по финансам"),
      "sh_hr":    ("Айгерім Бекқызы", "руководитель службы персонала"),
      "sh_prod":  ("Марат Нурланович", "начальник производства"),
    },
    "turns": [
      ("sh_chair", "RU+KZ", "Сәлеметсіздер ме, коллеги. Начинаем планерку. Бүгін үш вопрос: бюджет, график отпусков и обучение по охране труда. Динара Серікқызы, сізден бастайық."),
      ("sh_fin",   "RU",    "Спасибо. По бюджету ситуация такая: на ремонт не хватает примерно три миллиона тенге. Мы можем перераспределить средства, но нужно согласование."),
      ("sh_chair", "RU+KZ", "Жақсы. Динара Серікқызы, дайындаңыз обоснование на дополнительные три миллиона, до пятницы. Бұл өте маңызды."),
      ("sh_fin",   "RU",    "Хорошо, до пятницы подготовлю."),
      ("sh_chair", "RU+KZ", "Келесі мәселе, график отпусков. Айгерім, сізге сөз."),
      ("sh_hr",    "RU+KZ", "График отпусков на следующий год почти готов. Бірақ екі бөлім әлі мәлімет бермеді: производство и логистика."),
      ("sh_chair", "RU+KZ", "Марат, сіз производство бойынша мәліметті қашан бересіз?"),
      ("sh_prod",  "RU",    "Бауыржан Аскарович, завтра до обеда отправлю, без проблем."),
      ("sh_chair", "RU+KZ", "Жарайды. Марат, мәліметті ертеңге дейін Айгерімге жіберіңіз. Айгерім, после этого кестені всем руководителям жіберіңіз до конца недели."),
      ("sh_hr",    "RU+KZ", "Хорошо, жұманың соңына дейін жіберемін."),
      ("sh_chair", "RU+KZ", "Үшінші вопрос, обучение по охране труда. Марат, сколько сотрудников не прошли обучение?"),
      ("sh_prod",  "RU",    "Около сорока человек, в основном новые сотрудники цеха."),
      ("sh_chair", "RU+KZ", "Марат, организуйте обучение для них до пятнадцатого октября. Отчет маған жіберіңіз. Бәріне рақмет, на этом все."),
    ],
    "gold": [
      dict(id="SH-T01", description="Подготовить обоснование на дополнительные три миллиона тенге на ремонт",
           description_alt="Дайындаңыз обоснование на дополнительные три миллиона",
           assignee="Динара Серікқызы", deadline_raw="до пятницы", deadline_kind="relative_week", deadline_iso="2026-09-25",
           source_quote="Динара Серікқызы, дайындаңыз обоснование на дополнительные три миллиона, до пятницы."),
      dict(id="SH-T02", description="Передать данные производства для графика отпусков Айгерім Бекқызы",
           description_alt="Мәліметті Айгерімге жіберу",
           assignee="Марат Нурланович", deadline_raw="ертеңге дейін", deadline_kind="relative_day", deadline_iso="2026-09-24",
           source_quote="Марат, мәліметті ертеңге дейін Айгерімге жіберіңіз."),
      dict(id="SH-T03", description="Разослать график отпусков всем руководителям",
           description_alt="Кестені всем руководителям жіберу",
           assignee="Айгерім Бекқызы", deadline_raw="до конца недели", deadline_kind="relative_week", deadline_iso="2026-09-25",
           source_quote="Айгерім, после этого кестені всем руководителям жіберіңіз до конца недели."),
      dict(id="SH-T04", description="Организовать обучение по охране труда для сотрудников, не прошедших обучение (около 40 человек), и направить отчёт",
           description_alt="Организуйте обучение для них, отчет маған жіберіңіз",
           assignee="Марат Нурланович", deadline_raw="до пятнадцатого октября", deadline_kind="absolute_date", deadline_iso="2026-10-15",
           source_quote="Марат, организуйте обучение для них до пятнадцатого октября. Отчет маған жіберіңіз."),
    ],
  },
}


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def synth(text: str, spk: str, path: Path) -> np.ndarray:
    v = VOICES[spk]
    aiff = path.with_suffix(".aiff")
    # Нейроголос Aru иногда выдаёт сбойный рендер (повтор начала + обрезанный конец).
    # Синтезируем до совпадения длительностей двух прогонов (обычный рендер детерминирован).
    seen = []
    for k in range(6):
        cand = path.with_name(f"{path.stem}_try{k}.aiff")
        run(["say", "-v", v["voice"], "-r", str(v["rate"]), "-o", str(cand), text])
        info = sf.info(str(cand))
        seen.append((info.frames, cand))
        same = [c for f, c in seen if abs(f - info.frames) <= 50]
        if len(same) >= 2:
            os.replace(cand, aiff)
            break
    else:
        os.replace(max(seen, key=lambda fc: sum(abs(fc[0] - f) <= 50 for f, _ in seen))[1], aiff)
    sr_in = 22050
    p = v["pitch"]
    filters = []
    if abs(p - 1.0) > 1e-3:
        filters += [f"asetrate={int(sr_in * p)}", f"aresample={SR}", f"atempo={1.0 / p:.4f}"]
    else:
        filters += [f"aresample={SR}"]
    if v["extra"]:
        filters.append(v["extra"])
    # срезаем тишину по краям, чтобы границы реплик были точными
    filters += ["silenceremove=start_periods=1:start_threshold=-50dB",
                "areverse", "silenceremove=start_periods=1:start_threshold=-50dB", "areverse"]
    run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", str(aiff), "-af", ",".join(filters),
         "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(path)])
    a, sr = sf.read(str(path), dtype="float32")
    assert sr == SR
    return a


def build(name: str, m: dict, seed: int):
    rnd = random.Random(seed)
    parts, turns, t = [], [], 0.0
    lead = np.zeros(int(0.6 * SR), dtype=np.float32)
    parts.append(lead); t += len(lead) / SR
    for i, (spk, lang, text) in enumerate(m["turns"]):
        a = synth(text, spk, TMP / f"{name}_{i:02d}.wav")
        a = a / (np.max(np.abs(a)) + 1e-9) * rnd.uniform(0.55, 0.8)  # разная громкость говорящих
        start = t
        parts.append(a.astype(np.float32)); t += len(a) / SR
        pname, role = m["speakers"][spk]
        turns.append(dict(idx=i, speaker=spk, name=pname, role=role, voice=VOICES[spk]["voice"],
                          pitch=VOICES[spk]["pitch"], lang=lang, start=round(start, 3), end=round(t, 3), text=text))
        gap = np.zeros(int(rnd.uniform(0.4, 0.7) * SR), dtype=np.float32)
        parts.append(gap); t += len(gap) / SR
    parts.append(np.zeros(int(0.5 * SR), dtype=np.float32))
    audio = np.concatenate(parts)
    audio += np.random.default_rng(seed).normal(0, 0.0015, len(audio)).astype(np.float32)  # фоновый шум комнаты
    audio = np.clip(audio, -1, 1)
    wav = OUT / f"{name}.wav"
    sf.write(str(wav), audio, SR, subtype="PCM_16")
    run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "64k",
         str(OUT / f"{name}.mp3")])
    dur = len(audio) / SR

    (OUT / f"{name}.turns.json").write_text(json.dumps(
        {"meeting": name, "title": m["title"], "date": MEETING_DATE, "duration_s": round(dur, 2), "sample_rate": SR,
         "speakers": [dict(key=k, name=v[0], role=v[1], **{kk: VOICES[k][kk] for kk in ("voice", "pitch", "extra")})
                      for k, v in m["speakers"].items()],
         "turns": turns}, ensure_ascii=False, indent=1), encoding="utf-8")

    md = [f"# {m['title']}", "", f"Дата совещания (для относительных сроков): {MEETING_DATE} (среда). "
          f"Длительность: {dur:.1f} с. Синтез: macOS TTS (Aru kk_KZ, Milena ru_RU) + сдвиг тона/тембра ffmpeg.", "",
          "## Участники", "", "| Ключ | Имя | Роль | Голос |", "|---|---|---|---|"]
    for k, (pn, role) in m["speakers"].items():
        v = VOICES[k]
        md.append(f"| {k} | {pn} | {role} | {v['voice']}, pitch×{v['pitch']}{', ' + v['extra'] if v['extra'] else ''} |")
    md += ["", "## Сценарий", "", "| # | Время | Говорящий | Язык | Реплика |", "|---|---|---|---|---|"]
    for tr in turns:
        md.append(f"| {tr['idx']} | {tr['start']:.1f}–{tr['end']:.1f} | {tr['name']} | {tr['lang']} | {tr['text']} |")
    md += ["", "## Эталонные поручения", "", "| ID | Поручение | Ответственный | Срок | Дата |", "|---|---|---|---|---|"]
    for g in m["gold"]:
        md.append(f"| {g['id']} | {g['description']} | {g['assignee']} | {g['deadline_raw']} | {g['deadline_iso']} |")
    (OUT / f"{name}.script.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    chair = next(v[0] for k, v in m["speakers"].items() if k.endswith("chair"))
    tasks = []
    for g in m["gold"]:
        tasks.append(dict(id=g["id"], meeting=name, description=g["description"], description_alt=g.get("description_alt"),
                          assignee=g["assignee"], assignee_kind="person_present", co_assignees=[], issued_by=chair,
                          deadline_raw=g["deadline_raw"], deadline_kind=g["deadline_kind"], deadline_iso=g["deadline_iso"],
                          source_quote=g["source_quote"], appears_in_protocol_table=True))
    gold = {"meetings": [{"number": name, "title": m["title"], "date": MEETING_DATE, "chair": chair,
                          "participants": [{"name": v[0], "role": v[1], "present": True} for v in m["speakers"].values()],
                          "languages": sorted({tr["lang"] for tr in turns})}],
            "tasks": tasks}
    (OUT / f"{name}.gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{name}: {dur:.1f} s, {len(turns)} реплик, {len(m['speakers'])} говорящих, {len(tasks)} поручений")


build("kk_meeting", MEETINGS["kk_meeting"], seed=11)
build("shala_meeting", MEETINGS["shala_meeting"], seed=22)

# Объявление о записи (согласие участников): KZ (Aru) + RU (Milena)
kz = synth("Назар аударыңыздар, кеңес жазылып, жасанды интеллект арқылы транскрипцияланады.", "kk_fin", TMP / "consent_kz.wav")
ru = synth("Внимание, совещание записывается и расшифровывается с помощью ИИ. Обработка локальная.", "sh_fin", TMP / "consent_ru.wav")
z = lambda s: np.zeros(int(s * SR), dtype=np.float32)
c = np.concatenate([z(0.2), kz / (np.abs(kz).max() + 1e-9) * 0.8, z(0.5), ru / (np.abs(ru).max() + 1e-9) * 0.8, z(0.3)])
cw = TMP / "consent.wav"
sf.write(str(cw), c, SR, subtype="PCM_16")
run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", str(cw), "-c:a", "libmp3lame", "-b:a", "64k", "app/static/consent.mp3"])
print(f"app/static/consent.mp3: {len(c) / SR:.1f} s")
subprocess.run(["rm", "-rf", str(TMP)])
PYEOF
ls -la samples app/static/consent.mp3
