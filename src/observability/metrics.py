"""
src/observability/metrics.py
In-memory metrics collector.  Flush to JSON at end of run.
No external dependency — pure Python.

Usage:
    from src.observability.metrics import metrics
    metrics.inc("ocr_pages_processed")
    metrics.record("ocr_avg_confidence", 91.2)
    metrics.flush("output/metrics.json")
"""

import json
import os
import time
from collections import defaultdict
from threading import Lock


class _Metrics:
    def __init__(self):
        self._counters: dict[str, int] = defaultdict(int)
        self._samples:  dict[str, list[float]] = defaultdict(list)
        self._timers:   dict[str, float] = {}
        self._lock = Lock()

    def inc(self, key: str, amount: int = 1):
        with self._lock:
            self._counters[key] += amount

    def record(self, key: str, value: float):
        with self._lock:
            self._samples[key].append(value)

    def start_timer(self, key: str):
        self._timers[key] = time.perf_counter()

    def stop_timer(self, key: str):
        if key in self._timers:
            elapsed = time.perf_counter() - self._timers.pop(key)
            self.record(f"{key}_seconds", elapsed)

    def summary(self) -> dict:
        out = {"counters": dict(self._counters), "distributions": {}}
        for k, vals in self._samples.items():
            if vals:
                out["distributions"][k] = {
                    "count": len(vals),
                    "min":   min(vals),
                    "max":   max(vals),
                    "mean":  sum(vals) / len(vals),
                }
        return out

    def flush(self, path: str):
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.summary(), f, indent=2)


# Singleton
metrics = _Metrics()
