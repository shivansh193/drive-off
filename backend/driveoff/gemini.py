"""Thin Gemini client (Google AI Studio REST) with retries and an on-disk cache.

Used for three jobs: parsing the operator's rate sign, second-pass reading of low-confidence plate crops,
and reviewing dispute evidence. All three return JSON. Every call is cached by content hash so the
hosted demo replays instantly and stays within quota.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time

import requests

from . import config

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiUnavailable(Exception):
    pass


def available() -> bool:
    return bool(config.GEMINI_API_KEY)


def generate_json(prompt: str, images: list[bytes] | None = None, schema_hint: str = "", timeout=60,
                  use_cache=True) -> dict:
    parts = [{"text": prompt + (f"\n\nReturn JSON only, matching: {schema_hint}" if schema_hint else "")}]
    for img in images or []:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}})
    key = hashlib.sha256(json.dumps([config.GEMINI_MODEL, parts], sort_keys=True).encode()).hexdigest()
    cache = config.CACHE_DIR / f"gemini_{key[:32]}.json"
    if use_cache and cache.exists():
        out = json.loads(cache.read_text())
        out["_cached"] = True
        return out
    if not available():
        raise GeminiUnavailable("GEMINI_API_KEY not set")
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    last = None
    # Primary model first; on quota exhaustion (429) move to the next model, which has its own quota.
    models = [config.GEMINI_MODEL] + [m for m in config.GEMINI_FALLBACK_MODELS if m != config.GEMINI_MODEL]
    for model in models:
        for attempt in range(2):
            try:
                t0 = time.time()
                r = requests.post(URL.format(model=model), headers={"x-goog-api-key": config.GEMINI_API_KEY},
                                  json=body, timeout=timeout)
                if r.status_code == 429:
                    last = f"{model}: 429 quota"
                    break  # next model
                if r.status_code in (500, 503) and attempt == 0:
                    last = f"{model}: {r.status_code}"
                    time.sleep(2)
                    continue
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                out = json.loads(text)
                if isinstance(out, list):
                    out = {"items": out}
                out["_latency_ms"] = int((time.time() - t0) * 1000)
                out["_model"] = model
                cache.write_text(json.dumps(out))
                return out
            except (requests.RequestException, KeyError, ValueError) as e:
                last = f"{model}: {e}"
                time.sleep(1)
    raise GeminiUnavailable(f"Gemini failed: {last}")
