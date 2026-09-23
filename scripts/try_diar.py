"""Диаризация на наборе записей: число голосов, число реплик, время. Запуск: .venv/bin/python scripts/try_diar.py файлы..."""
import sys, time; sys.path.insert(0, ".")
from app.pipeline.audio import load_16k
from app.pipeline import diarize as D
for f in sys.argv[1:]:
    a = load_16k(f)
    t = time.time(); tr = D.diarize(a)
    dur = {}
    for x in tr:
        dur[x.speaker] = dur.get(x.speaker, 0) + x.end - x.start
    print(f.split('/')[-1][:24], f"{len(a)/16000:.0f}s → {len({x.speaker for x in tr})} голосов, {len(tr)} реплик, {time.time()-t:.1f}s;",
          "доли:", " ".join(f"{k}:{v:.0f}" for k, v in sorted(dur.items(), key=lambda kv: -kv[1])[:10]))
