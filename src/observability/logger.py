"""
src/observability/logger.py
Centralised structured logger.
Every pipeline event is logged as a structured dict so logs are
machine-parseable for metrics collection later.

Usage:
    from src.observability.logger import get_logger
    log = get_logger(__name__)
    log.info("ocr_complete", page=1, avg_conf=91.2, words=342)
"""

import logging
import json
import os
from datetime import datetime, timezone


class StructuredLogger:
    """Wraps stdlib logger with structured (JSON-line) emission."""

    def __init__(self, name: str, raw: logging.Logger):
        self._name = name
        self._raw = raw

    def _emit(self, level: str, event: str, **kwargs):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "logger": self._name,
            "level": level,
            "event": event,
            **kwargs,
        }
        msg = json.dumps(payload, default=str)
        getattr(self._raw, level)(msg)

    def debug(self, event: str, **kw):   self._emit("debug",   event, **kw)
    def info(self,  event: str, **kw):   self._emit("info",    event, **kw)
    def warning(self, event: str, **kw): self._emit("warning", event, **kw)
    def error(self, event: str, **kw):   self._emit("error",   event, **kw)
    def exception(self, event: str, **kw):
        import traceback
        self._emit("error", event, traceback=traceback.format_exc(), **kw)


_initialised = False


def setup_logging(log_file: str, level: str = "INFO") -> None:
    global _initialised
    if _initialised:
        return
    os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",          # raw — StructuredLogger adds its own envelope
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    _initialised = True


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(name, logging.getLogger(name))
