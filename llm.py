"""Ollama client with separate planning and fast-model profiles."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import time

import requests
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).with_name(".env"))

log = logging.getLogger("jarvs.llm")

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
PLAN_MODEL = os.environ.get("JARVIS_PLAN_MODEL", "dolphin-mistral:7b")
FAST_MODEL = os.environ.get("JARVIS_FAST_MODEL", "huihui_ai/qwen3-abliterated:4b")
KEEP_ALIVE = os.environ.get("JARVIS_LLM_KEEP_ALIVE", "5m")
DEFAULT_TIMEOUT = (
    float(os.environ.get("JARVIS_LLM_CONNECT_TIMEOUT", "5")),
    float(os.environ.get("JARVIS_LLM_READ_TIMEOUT", "300")),
)

def chat(role: str, system: str, user: str, think: bool = False,
         retries: int = 2, timeout: float | tuple[float, float] = DEFAULT_TIMEOUT) -> str:
    """One call to Ollama. Returns assistant text with <think> stripped."""
    model = PLAN_MODEL if think else FAST_MODEL
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": think,
        "keep_alive": KEEP_ALIVE,
        # These MUST be inside options. Top level is silently ignored by Ollama.
        "options": {
            "num_predict": -1,
            "temperature": 0.2 if think else (0.0 if role in ("act", "check") else 0.1),
        },
    }

    last = None
    attempt = 0
    fallback_used = False
    while attempt <= retries:
        t0 = time.time()
        try:
            r = requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            text = (data.get("message") or {}).get("content", "") or ""
            log.info("%s model=%s think=%s %.1fs",
                     role, model, think, time.time() - t0)
            return strip_think(text).strip()
        except Exception as exc:  # noqa: BLE001
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if (think and not fallback_used and model != FAST_MODEL
                    and status_code in (400, 404)):
                model = FAST_MODEL
                payload["model"] = model
                payload["think"] = False
                fallback_used = True
                log.warning("%s model rejected deep-planning payload; retrying with fast model=%s", role, model)
                continue
            last = exc
            log.warning("%s call failed (try %d): %s", role, attempt + 1, exc)
            if attempt >= retries:
                break
            time.sleep(2 ** attempt)
            attempt += 1
    raise RuntimeError(f"ollama failed after {retries + 1} tries: {last}")


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks some models emit inline."""
    while "<think>" in text and "</think>" in text:
        a = text.index("<think>")
        b = text.index("</think>") + len("</think>")
        text = text[:a] + text[b:]
    return text.replace("<think>", "").replace("</think>", "")


def chat_json(role: str, system: str, user: str, think: bool = False,
              default=None):
    """Same as chat() but parses JSON, tolerating markdown fences and prose."""
    raw = chat(role, system + "\nReply with JSON only. No prose, no fences.",
               user, think=think)
    txt = raw.strip()
    if "```" in txt:
        parts = txt.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") or p.startswith("["):
                txt = p
                break
    start = min([i for i in (txt.find("{"), txt.find("[")) if i != -1], default=-1)
    if start > 0:
        txt = txt[start:]
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        log.warning("%s: bad JSON, using default. raw=%.200s", role, raw)
        return default
