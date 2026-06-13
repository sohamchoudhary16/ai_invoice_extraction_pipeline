"""
src/preprocessing/language_detector.py
Lightweight language detection from extracted text.
Uses langdetect (pure Python, no server).
Falls back to 'de' (German) if uncertain — since the target is EU invoices.

Install: pip install langdetect
"""

from src.observability.logger import get_logger

log = get_logger(__name__)

_FALLBACK_LANG = "de"


def detect_language(text: str) -> str:
    """
    Detect language of text. Returns ISO 639-1 code ('de', 'en', 'fr' etc).
    Returns fallback language on short/noisy text.
    """
    if not text or len(text.strip()) < 30:
        log.debug("lang_detect_skipped", reason="text_too_short")
        return _FALLBACK_LANG
    try:
        from langdetect import detect, LangDetectException
        lang = detect(text)
        log.debug("lang_detected", lang=lang)
        return lang
    except ImportError:
        log.warning("lang_detect_unavailable", reason="langdetect not installed")
        return _FALLBACK_LANG
    except Exception as e:
        log.warning("lang_detect_failed", error=str(e))
        return _FALLBACK_LANG
