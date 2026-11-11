"""Thin Gemini client with retries, cheap-model fallback and an on-disk cache.

Backends:
  * vertex (default when GOOGLE_CLOUD_PROJECT is set): Vertex AI via Application Default Credentials
    (local `gcloud auth application-default login`, or a service-account JSON on Render).
  * studio: Google AI Studio with GEMINI_API_KEY.

Only low-cost Flash-Lite class models are used (config.GEMINI_MODELS). Used for three jobs: parsing the
operator's rate sign, second-pass reading of low-confidence plate crops, and reviewing dispute evidence.
Every call is cached by content hash so the hosted demo replays instantly and costs nothing on repeat.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time

import requests

from . import config

STUDIO_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
VERTEX_URL = ("https://{host}/v1/projects/{project}/locations/{loc}/publishers/google/models/{model}:generateContent")

_creds = None
_clock = threading.Lock()


class GeminiUnavailable(Exception):
    pass


def backend() -> str:
    if config.GEMINI_BACKEND in ("vertex", "studio"):
        return config.GEMINI_BACKEND
    return "vertex" if config.GOOGLE_CLOUD_PROJECT else ("studio" if config.GEMINI_API_KEY else "none")


def available() -> bool:
    return backend() != "none"


def _vertex_token() -> str:
    global _creds
    import google.auth
    import google.auth.transport.requests
    with _clock:
        if _creds is None:
            _creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not _creds.valid:
            _creds.refresh(google.auth.transport.requests.Request())
        return _creds.token


def _post(model: str, body: dict, timeout: int) -> requests.Response:
    if backend() == "vertex":
        loc = config.VERTEX_LOCATION
        host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
        url = VERTEX_URL.format(host=host, project=config.GOOGLE_CLOUD_PROJECT, loc=loc, model=model)
        return requests.post(url, headers={"Authorization": f"Bearer {_vertex_token()}"}, json=body, timeout=timeout)
    return requests.post(STUDIO_URL.format(model=model), headers={"x-goog-api-key": config.GEMINI_API_KEY},
                         json=body, timeout=timeout)


def generate_json(prompt: str, images: list[bytes] | None = None, schema_hint: str = "", timeout=60,
                  use_cache=True) -> dict:
    parts = [{"text": prompt + (f"\n\nReturn JSON only, matching: {schema_hint}" if schema_hint else "")}]
    for img in images or []:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}})
    key = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    cache = config.CACHE_DIR / f"gemini_{key[:32]}.json"
    if use_cache and cache.exists():
        out = json.loads(cache.read_text())
        out["_cached"] = True
        return out
    if not available():
        raise GeminiUnavailable("no Gemini backend configured")
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0, "maxOutputTokens": 1024}}
    last = None
    for model in config.GEMINI_MODELS:
        for attempt in range(2):
            try:
                t0 = time.time()
                r = _post(model, body, timeout)
                if r.status_code in (404, 429):
                    last = f"{model}: {r.status_code}"
                    break  # model missing or out of quota: next cheap model
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
                out["_backend"] = backend()
                cache.write_text(json.dumps(out))
                return out
            except Exception as e:  # auth errors, timeouts, bad JSON
                last = f"{model}: {type(e).__name__}: {str(e)[:160]}"
                time.sleep(1)
    raise GeminiUnavailable(f"Gemini failed: {last}")
