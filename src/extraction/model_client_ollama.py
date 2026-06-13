"""
src/extraction/model_client_ollama.py
Ollama API client with:
  - retry logic (max 3 attempts with backoff)
  - timeout handling
  - model versioning metadata attached to every response
  - temperature = 0 enforced for extraction (determinism)
"""

import json
import time
import requests
from src.observability.logger import get_logger
from src.observability.metrics import metrics

log = get_logger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY_S = 5     # seconds between retries


def call_ollama(
    system_prompt: str,
    user_prompt: str,
    base_url: str,
    model: str,
    temperature: float = 0.0,
    timeout: int = 120,
) -> tuple[str | None, dict]:
    """
    Call Ollama /api/chat endpoint.

    Returns
    -------
    (response_text_or_None, metadata_dict)

    metadata includes: model, attempt, latency_s, error
    """
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": temperature, "seed": 42},
        "messages": [
            {"role": "system",    "content": system_prompt},
            {"role": "user",      "content": user_prompt},
        ],
    }

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        t0 = time.perf_counter()
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            latency = round(time.perf_counter() - t0, 2)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("message", {}).get("content", "")
            metrics.record("llm_latency_s", latency)
            metrics.inc("llm_calls_success")
            log.info("llm_response_ok",
                     model=model, attempt=attempt,
                     latency_s=latency, chars=len(text))
            return text, {
                "model": model, "attempt": attempt,
                "latency_s": latency, "error": None,
            }

        except requests.exceptions.Timeout:
            last_error = f"TIMEOUT after {timeout}s"
        except requests.exceptions.ConnectionError:
            last_error = "OLLAMA_NOT_RUNNING — is `ollama serve` running?"
        except Exception as e:
            last_error = str(e)

        metrics.inc("llm_calls_retry")
        log.warning("llm_retry",
                    model=model, attempt=attempt,
                    error=last_error, next_in=_RETRY_DELAY_S)
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_DELAY_S)

    metrics.inc("llm_calls_failed")
    log.error("llm_failed", model=model, attempts=_MAX_RETRIES, error=last_error)
    return None, {
        "model": model, "attempt": _MAX_RETRIES,
        "latency_s": None, "error": last_error,
    }
