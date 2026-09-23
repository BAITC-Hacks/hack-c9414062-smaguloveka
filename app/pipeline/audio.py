"""Декодирование любого аудио/видео в 16 кГц mono float32 через ffmpeg."""
import subprocess
import numpy as np

SR = 16000


def load_16k(path: str) -> np.ndarray:
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()
