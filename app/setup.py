"""Мастер первого запуска: проверка и скачивание локальных моделей с прогрессом.
Модели скачиваются один раз (Hugging Face / GitHub Releases / реестр Ollama), дальше работа без сети."""
import json
import shutil
import threading
import urllib.request

from . import db, llm_settings
from .config import MODELS, OLLAMA_URL

HF = "https://huggingface.co/{repo}/resolve/main/{file}"
COMPONENTS = [
    {"id": "asr", "title": "Распознавание речи — GigaAM-Multilingual large CTC",
     "desc": "Русский, казахский и смешанная речь в одной модели (int8 ONNX, CPU). Лицензия MIT.", "size_mb": 592,
     "dir": "gigaam-ml-large-ctc",
     "files": [(HF.format(repo="istupakov/gigaam-multilingual-large-ctc-onnx", file=f), f)
               for f in ("config.json", "multilingual_vocab.txt", "multilingual_large_ctc.int8.onnx")]},
    {"id": "vad", "title": "Детектор речи — Silero VAD",
     "desc": "Находит речь в записи, в том числе тихую, с дальнего микрофона. Лицензия MIT.", "size_mb": 2,
     "dir": "silero-vad",
     "files": [(HF.format(repo="istupakov/silero-vad-onnx", file=f), f) for f in ("config.json", "silero_vad.onnx")]},
    {"id": "spk", "title": "Голосовые отпечатки — NVIDIA NeMo TitaNet-small",
     "desc": "Эмбеддинги голоса для диаризации: кто из участников говорит.", "size_mb": 38, "dir": "",
     "files": [("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                "nemo_en_titanet_small.onnx", "nemo_en_titanet_small.onnx")]},
]
OLLAMA_SUGGEST = [{"model": "gemma4:latest", "size_gb": 9.6, "note": "лучшее качество, нужно ~10 ГБ RAM"},
                  {"model": "qwen2.5:7b", "size_gb": 4.7, "note": "баланс качества и скорости"},
                  {"model": "qwen2.5:3b", "size_gb": 1.9, "note": "быстро, для слабых машин"}]

_state: dict[str, dict] = {}
_pull: dict = {}
_lock = threading.Lock()


def installed(c: dict) -> bool:
    d = MODELS / c["dir"]
    return all((d / name).exists() and (d / name).stat().st_size > 0 for _, name in c["files"])


def _download(c: dict):
    st = _state[c["id"]]
    try:
        d = MODELS / c["dir"]
        d.mkdir(parents=True, exist_ok=True)
        todo = [(u, n) for u, n in c["files"] if not (d / n).exists()]
        sizes = []
        for u, _ in todo:  # общий размер для прогресса
            try:
                with urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=20) as r:
                    sizes.append(int(r.headers.get("Content-Length") or 0))
            except Exception:
                sizes.append(0)
        total = sum(sizes) or c["size_mb"] * 1_000_000
        done = 0
        for (u, n), _ in zip(todo, sizes):
            part = d / (n + ".part")
            with urllib.request.urlopen(u, timeout=60) as r, open(part, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    st["pct"] = min(99, int(100 * done / total))
            part.rename(d / n)
        st.update(state="done", pct=100)
    except Exception as e:
        st.update(state="error", error=f"{type(e).__name__}: {e}")


def start_download(cid: str) -> dict:
    with _lock:
        for c in COMPONENTS:
            if cid not in ("all", c["id"]) or installed(c):
                continue
            if _state.get(c["id"], {}).get("state") == "downloading":
                continue
            _state[c["id"]] = {"state": "downloading", "pct": 0, "error": None}
            threading.Thread(target=_download, args=(c,), daemon=True).start()
    return status()


def _ollama_pull(model: str):
    try:
        req = urllib.request.Request(OLLAMA_URL.rstrip("/") + "/api/pull",
                                     data=json.dumps({"model": model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise RuntimeError(ev["error"])
                if ev.get("total"):
                    _pull["pct"] = int(100 * (ev.get("completed") or 0) / ev["total"])
                _pull["phase"] = ev.get("status", "")
        _pull.update(state="done", pct=100)
    except Exception as e:
        _pull.update(state="error", error=f"{type(e).__name__}: {e}")


def start_ollama_pull(model: str) -> dict:
    if _pull.get("state") != "downloading":
        _pull.clear()
        _pull.update(model=model, state="downloading", pct=0, phase="", error=None)
        threading.Thread(target=_ollama_pull, args=(model,), daemon=True).start()
    return status()


def status() -> dict:
    comps = []
    for c in COMPONENTS:
        st = _state.get(c["id"], {})
        ok = installed(c)
        comps.append({"id": c["id"], "title": c["title"], "desc": c["desc"], "size_mb": c["size_mb"], "installed": ok,
                      "state": "done" if ok else st.get("state", "missing"), "pct": 100 if ok else st.get("pct", 0),
                      "error": st.get("error")})
    models = llm_settings.ollama_models()
    llm = llm_settings.public()
    llm_ready = (llm["provider"] == "ollama" and llm["ollama_model"] in models) or \
                (llm["provider"] == "openai" and llm["key_set"])
    return {"onboarded": bool(db.kv_get("onboarded", False)), "models_ready": all(c["installed"] for c in comps),
            "llm_ready": llm_ready, "ready": all(c["installed"] for c in comps) and llm_ready,
            "disk_free_gb": round(shutil.disk_usage(MODELS).free / 1e9, 1), "components": comps,
            "ollama": {"running": bool(models) or _ollama_alive(), "models": models, "suggest": OLLAMA_SUGGEST,
                       "pull": dict(_pull)}, "llm": llm}


def _ollama_alive() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL.rstrip("/") + "/api/version", timeout=2):
            return True
    except Exception:
        return False


def complete(done: bool = True) -> dict:
    db.kv_set("onboarded", bool(done))
    return status()
