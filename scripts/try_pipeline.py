import sys, time
sys.path.insert(0, ".")
from app.pipeline.audio import load_16k
from app.pipeline.diarize import diarize
from app.pipeline.asr import transcribe
t0 = time.time(); a = load_16k(sys.argv[1]); print("decode", round(time.time()-t0, 2), "s", len(a)/16000, "s audio")
n = int(sys.argv[2]) if len(sys.argv) > 2 else -1
t0 = time.time(); turns = diarize(a, n); print("diar", round(time.time()-t0, 2), "s", len(turns), "turns", len({t.speaker for t in turns}), "speakers")
t0 = time.time()
for t in turns:
    print(f"[{t.start:6.1f}-{t.end:6.1f}] S{t.speaker}: {transcribe(a, t.start, t.end)}")
print("asr", round(time.time()-t0, 2), "s")
