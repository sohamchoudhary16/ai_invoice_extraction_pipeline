"""
src/guardrails/input_sanitizer.py
Sanitizes OCR text before it is injected into LLM prompts.

Defends against:
  1. Prompt injection — instructions hidden in document text
  2. Control characters and broken encodings
  3. Oversized inputs that blow the context window
  4. Malicious unicode tricks (bidirectional overrides etc.)
"""

import re
import unicodedata
from src.observability.logger import get_logger

log = get_logger(__name__)

# Max characters we ever pass to the LLM for a single page
_MAX_CHARS = 6_000

# Phrases that are clear injection attempts
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    re.compile(r"forget\s+(all\s+)?previous\s+instructions?", re.I),
    re.compile(r"you\s+are\s+now\s+a", re.I),
    re.compile(r"act\s+as\s+(a\s+)?(?:different|new)", re.I),
    re.compile(r"disregard\s+(your\s+)?system\s+prompt", re.I),
    re.compile(r"<\s*system\s*>", re.I),
    re.compile(r"\[INST\]", re.I),
    re.compile(r"###\s*instruction", re.I),
    re.compile(r"mark\s+(this\s+)?invoice\s+as\s+(paid|approved|processed)", re.I),
]

# Unicode categories to strip (control chars etc.)
_STRIP_CATEGORIES = {"Cc", "Cf", "Cs", "Co"}


def sanitize(text: str, source: str = "unknown") -> tuple[str, list[str]]:
    """
    Clean OCR text for safe LLM injection.

    Returns
    -------
    (cleaned_text, warnings)
      warnings is a list of issue descriptions (empty = clean)
    """
    warnings: list[str] = []

    # 1. Strip dangerous unicode categories
    cleaned = "".join(
        ch for ch in text
        if unicodedata.category(ch) not in _STRIP_CATEGORIES
    )

    # 2. Normalise to NFC
    cleaned = unicodedata.normalize("NFC", cleaned)

    # 3. Detect injection attempts
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(cleaned):
            snippet = pattern.search(cleaned).group(0)[:60]
            warnings.append(f"INJECTION_DETECTED: '{snippet}'")
            log.warning("injection_detected", source=source, snippet=snippet)
            # Redact the injection phrase rather than dropping the whole text
            cleaned = pattern.sub("[REDACTED]", cleaned)

    # 4. Truncate
    if len(cleaned) > _MAX_CHARS:
        warnings.append(f"TEXT_TRUNCATED: {len(cleaned)} → {_MAX_CHARS} chars")
        cleaned = cleaned[:_MAX_CHARS]

    return cleaned, warnings
