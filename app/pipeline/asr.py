"""Распознавание речи: GigaAM-Multilingual large CTC (ru + kk в одной модели, MIT), int8 ONNX на CPU."""
import numpy as np
import onnx_asr

from ..config import MODELS
from .audio import SR

_model = None
MAX_CHUNK_S = 20.0


def model():
    global _model
    if _model is None:
        import os
        if (MODELS / "gigaam-ml-large-ctc" / "multilingual_large_ctc.int8.onnx").exists():
            os.environ["HF_HUB_OFFLINE"] = "1"  # модель уже скачана — работаем без сети (закрытый контур)
        _model = onnx_asr.load_model("gigaam-multilingual-large-ctc", str(MODELS / "gigaam-ml-large-ctc"),
                                     quantization="int8", providers=["CPUExecutionProvider"])
    return _model


def _split(chunk: np.ndarray) -> list[np.ndarray]:
    """Длинную реплику режем по самым тихим местам на куски не длиннее MAX_CHUNK_S."""
    n = int(MAX_CHUNK_S * SR)
    if len(chunk) <= n:
        return [chunk]
    parts, pos = [], 0
    win = int(0.2 * SR)
    while len(chunk) - pos > n:
        lo, hi = pos + int(n * 0.6), pos + n
        seg = chunk[lo:hi]
        energy = np.convolve(seg ** 2, np.ones(win) / win, mode="same")
        cut = lo + int(np.argmin(energy))
        parts.append(chunk[pos:cut])
        pos = cut
    parts.append(chunk[pos:])
    return parts


def transcribe(audio: np.ndarray, start: float, end: float, pad: float = 0.15) -> str:
    a = max(0, int((start - pad) * SR))
    b = min(len(audio), int((end + pad) * SR))
    texts = []
    for piece in _split(audio[a:b]):
        if len(piece) < int(0.3 * SR):
            continue
        texts.append(model().recognize(piece, sample_rate=SR).strip())
    return " ".join(t for t in texts if t)
