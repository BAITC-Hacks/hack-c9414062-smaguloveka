"""Пути и настройки. Всё локально: модели в models/, данные в data/."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
DATA = Path(os.getenv("HATSHY_DATA", ROOT / "data"))
UPLOADS = DATA / "uploads"
OUTBOX = DATA / "outbox"
DB_PATH = DATA / "hatshy.db"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "gemma4:latest")
ASR_THREADS = int(os.getenv("ASR_THREADS", "4"))

for p in (DATA, UPLOADS, OUTBOX):
    p.mkdir(parents=True, exist_ok=True)
