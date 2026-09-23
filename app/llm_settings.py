"""Выбор LLM для поручений и саммари: локальная Ollama (по умолчанию, закрытый контур)
или OpenAI-совместимый API (OpenAI или свой vLLM-сервер). ASR и диаризация всегда локальные.
Ключ хранится только в локальной БД (data/, в .gitignore) или в переменной OPENAI_API_KEY и наружу по API не отдаётся."""
import json
import os
import urllib.error
import urllib.request

from . import db
from .config import OLLAMA_URL, LLM_MODEL

DEFAULT = {"provider": "ollama", "ollama_model": LLM_MODEL,
           "openai_base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
           "openai_model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), "openai_key": ""}


def get() -> dict:
    cfg = {**DEFAULT, **(db.kv_get("llm", {}) or {})}
    if not cfg.get("openai_key"):
        cfg["openai_key"] = os.getenv("OPENAI_API_KEY", "")
    return cfg


def public(cfg: dict | None = None) -> dict:
    cfg = cfg or get()
    k = cfg.get("openai_key") or ""
    return {"provider": cfg["provider"], "ollama_url": OLLAMA_URL, "ollama_model": cfg["ollama_model"],
            "openai_base_url": cfg["openai_base_url"], "openai_model": cfg["openai_model"],
            "key_set": bool(k), "key_hint": (k[:3] + "…" + k[-4:]) if len(k) > 10 else ("задан" if k else ""),
            "external": cfg["provider"] == "openai" and "api.openai.com" in cfg["openai_base_url"]}


def save(body: dict) -> dict:
    cur = db.kv_get("llm", {}) or {}
    if body.get("provider") in ("ollama", "openai"):
        cur["provider"] = body["provider"]
    for k in ("ollama_model", "openai_base_url", "openai_model"):
        if isinstance(body.get(k), str) and body[k].strip():
            cur[k] = body[k].strip().rstrip("/") if k == "openai_base_url" else body[k].strip()
    if body.get("clear_key"):
        cur["openai_key"] = ""
    elif isinstance(body.get("openai_key"), str) and body["openai_key"].strip():
        cur["openai_key"] = body["openai_key"].strip()
    db.kv_set("llm", cur)
    return public()


def _get_json(url: str, headers: dict | None = None, timeout: float = 8):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ollama_models() -> list[str]:
    try:
        return sorted(m["name"] for m in _get_json(OLLAMA_URL.rstrip("/") + "/api/tags", timeout=3).get("models", []))
    except Exception:
        return []


def test(body: dict | None = None) -> dict:
    """Проверка подключения с текущими (или переданными, ещё не сохранёнными) параметрами."""
    cfg = {**get(), **{k: v for k, v in (body or {}).items() if isinstance(v, str) and v.strip()}}
    if cfg["provider"] == "ollama":
        models = ollama_models()
        if not models:
            return {"ok": False, "error": f"Ollama недоступна по адресу {OLLAMA_URL}", "models": []}
        ok = cfg["ollama_model"] in models
        return {"ok": ok, "models": models,
                "error": None if ok else f"Модель {cfg['ollama_model']} не найдена в Ollama"}
    if not cfg.get("openai_key"):
        return {"ok": False, "error": "Не задан API-ключ", "models": []}
    try:
        data = _get_json(cfg["openai_base_url"].rstrip("/") + "/models",
                         {"Authorization": f"Bearer {cfg['openai_key']}"})
        models = sorted(m["id"] for m in data.get("data", []))
        ok = not models or cfg["openai_model"] in models
        return {"ok": ok, "models": models,
                "error": None if ok else f"Модель {cfg['openai_model']} недоступна для этого ключа"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read()[:200].decode(errors='ignore')}", "models": []}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}
