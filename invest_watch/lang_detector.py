"""Language detection on the first 300 characters (specs §4.3 / F8).

Primary: ``lingua`` (French, English, Chinese). Fallback: ``langdetect``.
Returns a lowercase ISO 639-1 code (BCP-47 language subtag).
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

WINDOW = 300

_ISO_ALIASES = {
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
    "iw": "he",
}


@lru_cache(maxsize=1)
def _lingua_detector():
    from lingua import Language, LanguageDetectorBuilder

    return (
        LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.FRENCH, Language.CHINESE)
        .with_preloaded_language_models()
        .build()
    )


def _normalise_code(code: str | None) -> str | None:
    if not code:
        return None
    lower = code.strip().lower().replace("_", "-")
    if lower in _ISO_ALIASES:
        return _ISO_ALIASES[lower]
    primary = lower.split("-", 1)[0]
    return primary or None


def detect_language(text: str | None) -> str | None:
    """Detect language of ``text``; ``None`` when empty or unrecognised."""
    sample = (text or "").strip()[:WINDOW]
    if len(sample) < 20:
        return None
    try:
        language = _lingua_detector().detect_language_of(sample)
        if language is not None:
            iso = getattr(language.iso_code_639_1, "name", None)
            code = _normalise_code(iso)
            if code:
                return code
    except Exception as exc:  # noqa: BLE001 — fallback path
        logger.debug("lingua failed: %s", exc)
    try:
        from langdetect import LangDetectException, detect

        return _normalise_code(detect(sample))
    except (LangDetectException, Exception) as exc:  # noqa: BLE001
        logger.debug("langdetect failed: %s", exc)
        return None
