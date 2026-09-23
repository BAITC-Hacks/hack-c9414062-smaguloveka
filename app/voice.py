"""Голосовые профили (идентификация участника по тембру).
Профиль — усреднённый нормированный эмбеддинг голоса NeMo TitaNet-small (та же модель, что в диаризации).
Создаётся из реплик участника в уже обработанном совещании или из записи с микрофона (~20 с).
При обработке нового совещания голос каждого найденного говорящего сравнивается с профилями (косинусное сходство)."""
import numpy as np

from . import db
from .pipeline.audio import load_16k, SR
from .pipeline.diarize import embed, silero_segments, vad_segments

MATCH_THRESHOLD = 0.65  # минимальное сходство голоса с профилем (разные люди в тестах: до 0.59)
MIN_MARGIN = 0.05       # отрыв от второго по сходству профиля
MIN_SPEECH_S = 5.0

SCHEMA = """CREATE TABLE IF NOT EXISTS voice_profiles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, key TEXT, embedding TEXT, seconds REAL, source TEXT,
  meeting_id INTEGER, created TEXT DEFAULT (datetime('now','localtime')))"""


def init():
    with db.conn() as c:
        c.execute(SCHEMA)


def key_of(name: str) -> str:
    fold = str.maketrans({"қ": "к", "ғ": "г", "ү": "у", "ұ": "у", "ә": "а", "ө": "о", "і": "и", "ң": "н", "һ": "х", "ё": "е"})
    return (name or "").lower().translate(fold).split()[0][:4] if (name or "").strip() else ""


def _mean_embedding(audio: np.ndarray, spans: list[tuple[float, float]], max_s: float = 60.0):
    vecs, weights, total = [], [], 0.0
    for s, e in sorted(spans, key=lambda x: x[0] - x[1]):  # сначала длинные фрагменты
        if e - s < 1.0:
            continue
        seg = audio[int(s * SR):int(e * SR)]
        vecs.append(embed(seg))
        weights.append(e - s)
        total += e - s
        if total >= max_s:
            break
    if total < MIN_SPEECH_S:
        raise ValueError(f"мало речи для профиля: {total:.0f} с (нужно не меньше {MIN_SPEECH_S:.0f} с)")
    v = np.average(np.stack(vecs), axis=0, weights=weights)
    return v / (np.linalg.norm(v) + 1e-9), total


def _save(name: str, vec: np.ndarray, seconds: float, source: str, meeting_id=None) -> dict:
    k = key_of(name)
    db.execute("DELETE FROM voice_profiles WHERE key=?", (k,))
    pid = db.insert("voice_profiles", name=name.strip(), key=k, embedding=vec.round(6).tolist(), seconds=round(seconds, 1),
                    source=source, meeting_id=meeting_id)
    return {"id": pid, "name": name.strip(), "seconds": round(seconds, 1), "source": source}


def from_meeting(name: str, meeting_id: int | None = None) -> dict:
    """Профиль из реплик участника в обработанном совещании (по умолчанию — в последнем, где он говорил)."""
    k = key_of(name)
    rows = db.q("SELECT s.meeting_id, s.label, s.name FROM speakers s JOIN meetings m ON m.id=s.meeting_id "
                "WHERE s.name IS NOT NULL ORDER BY s.meeting_id DESC")
    cand = [r for r in rows if key_of(r["name"]) == k and (meeting_id is None or r["meeting_id"] == meeting_id)]
    if not cand:
        raise ValueError("участник не найден среди говорящих обработанных совещаний")
    r = cand[0]
    m = db.one("SELECT audio_path FROM meetings WHERE id=?", (r["meeting_id"],))
    segs = db.q('SELECT start, "end" FROM segments WHERE meeting_id=? AND speaker=?', (r["meeting_id"], r["label"]))
    audio = load_16k(m["audio_path"])
    vec, secs = _mean_embedding(audio, [(s["start"], s["end"]) for s in segs])
    return _save(name, vec, secs, "meeting", r["meeting_id"])


def from_range(name: str, meeting_id: int, start: float, end: float) -> dict:
    """Профиль по фрагменту записи совещания, выделенному пользователем на шкале [start, end] (секунды)."""
    m = db.one("SELECT audio_path FROM meetings WHERE id=?", (meeting_id,))
    if not m or not m["audio_path"]:
        raise ValueError("совещание или его аудио не найдено")
    if end - start < 2:
        raise ValueError("выделите фрагмент длиннее 2 секунд")
    audio = load_16k(m["audio_path"])
    clip = audio[max(0, int(start * SR)):min(len(audio), int(end * SR))]
    spans = silero_segments(clip) or vad_segments(clip)
    vec, secs = _mean_embedding(clip, spans)
    return _save(name, vec, secs, "meeting", meeting_id)


def from_recording(name: str, path: str) -> dict:
    """Профиль из записи с микрофона: берём только речь (VAD)."""
    audio = load_16k(path)
    spans = silero_segments(audio) or vad_segments(audio)
    vec, secs = _mean_embedding(audio, spans)
    return _save(name, vec, secs, "mic")


def profiles() -> list[dict]:
    import json
    rows = db.q("SELECT id, name, key, embedding, seconds, source, meeting_id, created FROM voice_profiles ORDER BY name")
    for r in rows:
        if isinstance(r["embedding"], str):
            r["embedding"] = json.loads(r["embedding"])
    return rows


def match_speakers(audio: np.ndarray, segs: list[dict]) -> dict[str, dict]:
    """{speaker_label: {name, sim}} — голоса, узнанные по профилям (жадно, один профиль — один говорящий)."""
    profs = [p for p in profiles() if p.get("embedding")]
    if not profs:
        return {}
    P = np.array([p["embedding"] for p in profs], dtype=np.float32)
    by_lab: dict[str, list] = {}
    for s in segs:
        by_lab.setdefault(s["speaker"], []).append((s["start"], s["end"]))
    cents = {}
    for lab, spans in by_lab.items():
        try:
            cents[lab], _ = _mean_embedding(audio, spans, max_s=40.0)
        except ValueError:
            continue
    sims = {lab: P @ v for lab, v in cents.items()}
    pairs = sorted(((float(sims[lab][j]), lab, j) for lab in sims for j in range(len(profs))), reverse=True)
    out, used = {}, set()
    for sim, lab, j in pairs:
        if sim < MATCH_THRESHOLD:
            break
        if lab in out or j in used:
            continue
        second = max([float(x) for i, x in enumerate(sims[lab]) if i != j] or [0.0])
        if sim - second < MIN_MARGIN:
            continue
        out[lab] = {"name": profs[j]["name"], "sim": round(sim, 2)}
        used.add(j)
    return out
