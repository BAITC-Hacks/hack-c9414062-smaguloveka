"""Диаризация без облака и без torch.

Схема: энергетический VAD режет запись по паузам → для каждого фрагмента голосовой эмбеддинг
NeMo TitaNet-small (sherpa-onnx) → агломеративная кластеризация (косинус, average linkage)
с известным или автоматически найденным числом говорящих → склейка соседних фрагментов одного голоса.
Эти же эмбеддинги годятся для голосовых профилей (идентификация по тембру)."""
from dataclasses import dataclass
import numpy as np
import sherpa_onnx as so

from ..config import MODELS
from .audio import SR


@dataclass
class Turn:
    start: float
    end: float
    speaker: int


_extractor = None


def extractor():
    global _extractor
    if _extractor is None:
        cfg = so.SpeakerEmbeddingExtractorConfig(model=str(MODELS / "nemo_en_titanet_small.onnx"), num_threads=4)
        _extractor = so.SpeakerEmbeddingExtractor(cfg)
    return _extractor


def embed(audio: np.ndarray) -> np.ndarray:
    ex = extractor()
    st = ex.create_stream()
    st.accept_waveform(SR, audio)
    st.input_finished()
    v = np.array(ex.compute(st), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def silero_segments(audio: np.ndarray, max_len: float = 20.0) -> list[tuple[float, float]] | None:
    """Нейросетевой VAD Silero (sherpa-onnx): находит тихую речь дальнего микрофона, которую теряет энергетический VAD."""
    path = MODELS / "silero-vad" / "silero_vad.onnx"
    if not path.exists():
        return None
    cfg = so.VadModelConfig()
    cfg.silero_vad.model = str(path)
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_silence_duration = 0.15
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.silero_vad.max_speech_duration = max_len
    cfg.sample_rate = SR
    vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=max(60, int(max_len * 3)))
    out = []

    def drain():
        while not vad.empty():
            seg = vad.front
            out.append((round(seg.start / SR, 2), round((seg.start + len(seg.samples)) / SR, 2)))
            vad.pop()
    for i in range(0, len(audio), 512):
        vad.accept_waveform(audio[i:i + 512])
        drain()
    vad.flush()
    drain()
    return out


def vad_segments(audio: np.ndarray, min_sil: float = 0.3, min_len: float = 0.25) -> list[tuple[float, float]]:
    """Энергетический VAD с адаптивным порогом (уровень шума + 14 дБ, но не ниже -55 dBFS) — запасной вариант."""
    hop, win = int(0.01 * SR), int(0.03 * SR)
    n = max(1, (len(audio) - win) // hop)
    idx = np.arange(n)[:, None] * hop + np.arange(win)[None, :]
    rms = np.sqrt(np.mean(audio[idx] ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    floor = np.percentile(db, 10)
    thr = max(floor + 14, -55.0) if floor > -80 else -50.0
    speech = db > thr
    segs, start, sil = [], None, 0
    min_sil_f = int(min_sil / 0.01)
    for i, s in enumerate(speech):
        if s:
            if start is None:
                start = i
            sil = 0
        elif start is not None:
            sil += 1
            if sil >= min_sil_f:
                segs.append((start, i - sil + 1))
                start, sil = None, 0
    if start is not None:
        segs.append((start, n))
    out = []
    for a, b in segs:
        s, e = a * 0.01, b * 0.01 + 0.03
        if e - s >= min_len:
            out.append((round(s, 2), round(e, 2)))
    return out


def _agglomerate(X: np.ndarray, n_clusters: int | None, dist_thr: float, w: np.ndarray | None = None) -> np.ndarray:
    """Average-linkage по косинусному сходству, векторно (формула Ланса–Уильямса): O(n²) на слияние в numpy."""
    n = len(X)
    S = (X @ X.T).astype(np.float64)
    np.fill_diagonal(S, -np.inf)
    size = np.ones(n) if w is None else w.astype(np.float64).copy()
    labels = np.arange(n)
    k = n
    target = max(1, n_clusters or 1)
    while k > target:
        idx = int(np.argmax(S))
        i, j = divmod(idx, n)
        if n_clusters is None and 1 - S[i, j] > dist_thr:
            break
        new = (size[i] * S[i] + size[j] * S[j]) / (size[i] + size[j])
        S[i, :] = new
        S[:, i] = new
        S[i, i] = -np.inf
        S[j, :] = -np.inf
        S[:, j] = -np.inf
        size[i] += size[j]
        labels[labels == j] = i
        k -= 1
    _, inv = np.unique(labels, return_inverse=True)
    return inv


def diarize(audio: np.ndarray, num_speakers: int = -1, dist_thr: float = 0.45, min_share: float = 0.02) -> list[Turn]:
    segs = silero_segments(audio)
    if segs is None:
        segs = vad_segments(audio)
    if not segs:
        return []
    long_ids = [i for i, (s, e) in enumerate(segs) if e - s >= 1.0]
    if len(long_ids) < 2:
        return merge_turns([Turn(s, e, 0) for s, e in segs])
    X = np.stack([embed(audio[int(segs[i][0] * SR):int(segs[i][1] * SR)]) for i in long_ids])
    dur = np.array([segs[i][1] - segs[i][0] for i in long_ids])
    n = num_speakers if num_speakers and num_speakers > 0 else None
    lab = _agglomerate(X, min(n, len(X)) if n else None, dist_thr, w=dur)
    if n is None:  # мелкие кластеры (шум, «угу», эмоции) поглощаются ближайшим крупным голосом
        tot = dur.sum()
        big = [k for k in range(lab.max() + 1) if dur[lab == k].sum() >= max(6.0, min_share * tot)]
        if big:
            cb = np.stack([X[lab == k].mean(0) for k in big])
            cb /= np.linalg.norm(cb, axis=1, keepdims=True)
            for j in range(len(lab)):
                if lab[j] not in big:
                    lab[j] = big[int(np.argmax(cb @ X[j]))]
            _, lab = np.unique(lab, return_inverse=True)
    cents = np.stack([X[lab == k].mean(0) for k in range(lab.max() + 1)])
    cents /= np.linalg.norm(cents, axis=1, keepdims=True)
    labels = {}
    for j, i in enumerate(long_ids):
        labels[i] = int(lab[j])
    for i, (s, e) in enumerate(segs):  # короткие фрагменты — к ближайшему центроиду
        if i in labels:
            continue
        chunk = audio[int(s * SR):int(e * SR)]
        if e - s >= 0.5:
            labels[i] = int(np.argmax(cents @ embed(chunk)))
        else:
            labels[i] = labels.get(i - 1, 0)
    pieces = []
    for i, (s, e) in enumerate(segs):
        pieces += split_changes(audio, s, e, labels[i], cents)
    turns = merge_turns(pieces)
    order = {}
    for t in turns:  # нумерация по первому появлению
        order.setdefault(t.speaker, len(order))
    for t in turns:
        t.speaker = order[t.speaker]
    return turns


def split_changes(audio, s, e, lab, cents, win=1.5, hop=0.5, min_run=3, margin=0.08) -> list[Turn]:
    """Смена говорящего внутри фрагмента без паузы (склейка реплик): скользящее окно эмбеддингов,
    отрезок из >= min_run окон, уверенно ближе к другому центроиду, выделяется в отдельную реплику."""
    if e - s < 2 * win + hop * min_run or len(cents) < 2:
        return [Turn(s, e, lab)]
    starts = np.arange(s, e - win + 1e-6, hop)
    ass = []
    for t in starts:
        v = embed(audio[int(t * SR):int((t + win) * SR)])
        sims = cents @ v
        best = int(np.argmax(sims))
        second = np.sort(sims)[-2]
        ass.append(best if best != lab and sims[best] - max(second, sims[lab]) >= -1e-9 and sims[best] - sims[lab] > margin else lab)
    out, cur, run_start = [], lab, s
    i = 0
    while i < len(ass):
        if ass[i] != cur:
            j = i
            while j < len(ass) and ass[j] == ass[i]:
                j += 1
            if j - i >= min_run or ((i == 0 or j == len(ass)) and j - i >= 2):
                cut = float(starts[i] + win / 2) if i > 0 else s
                if cut - run_start > 0.3:
                    out.append(Turn(run_start, cut, cur))
                cur, run_start = ass[i], cut
            i = j
        else:
            i += 1
    out.append(Turn(run_start, e, cur))
    return out


def merge_turns(turns: list[Turn], gap: float = 1.2, max_len: float = 25.0) -> list[Turn]:
    out: list[Turn] = []
    for t in turns:
        if out and out[-1].speaker == t.speaker and t.start - out[-1].end <= gap and t.end - out[-1].start <= max_len:
            out[-1].end = max(out[-1].end, t.end)
        else:
            out.append(Turn(t.start, t.end, t.speaker))
    return out
