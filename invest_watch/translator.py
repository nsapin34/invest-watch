"""Machine translation to French (Microsoft Translator / Azure AI Translator v3).

Used at collection time to store a French copy (``title_fr`` / ``content_fr``)
of articles written in the languages listed in ``[translation] languages``.

Design (same spirit as the LLM client - the run never stops):
- secrets from the environment only: ``AZURE_TRANSLATOR_KEY`` and
  ``AZURE_TRANSLATOR_REGION`` (``.env``);
- per-run character budget (``max_chars_per_run``) protecting the free F0 tier
  (2 M characters / month); once reached, articles are stored untranslated;
- long texts are cut to ``max_chars_per_article`` then split on sentence
  boundaries into chunks below the per-request limit;
- 429 / 5xx are retried with backoff; any other failure returns ``None``
  (degraded: the article is simply stored without translation).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

AZURE_TRANSLATOR_URL = "https://api.cognitive.microsofttranslator.com/translate"
CHUNK_CHARS = 9000  # well below the 50 000 characters/request API limit
_SENTENCE_END = re.compile(r"(?<=[\u3002\uff01\uff1f.!?\n])")
_RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class TranslationSection:
    """Settings from the ``[translation]`` TOML section."""

    enabled: bool = False
    provider: str = "azure"
    target: str = "fr"
    languages: tuple[str, ...] = ("zh",)
    max_chars_per_run: int = 60_000
    max_chars_per_article: int = 10_000
    timeout_seconds: int = 30
    max_retries: int = 2


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split ``text`` into chunks <= ``limit`` chars, on sentence ends when possible."""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for piece in _SENTENCE_END.split(text):
        while len(piece) > limit:  # a single endless "sentence": hard cut
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        if len(current) + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks


class AzureTranslator:
    """Minimal Azure AI Translator v3 client with a per-run character budget."""

    def __init__(
        self,
        cfg: TranslationSection,
        *,
        api_key: str | None = None,
        region: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self._key = api_key if api_key is not None else os.environ.get("AZURE_TRANSLATOR_KEY", "")
        self._region = region if region is not None else os.environ.get("AZURE_TRANSLATOR_REGION", "")
        self._client = client or httpx.Client(timeout=cfg.timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep
        self.chars_used = 0
        self.translated = 0
        self.failures = 0
        self.budget_exhausted = False
        self.disabled_reason: str | None = None
        if not self._key:
            self.disabled_reason = "AZURE_TRANSLATOR_KEY missing"

    # -- public ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.disabled_reason is None

    def wants(self, language: str | None) -> bool:
        """True when articles in ``language`` must be translated."""
        if not self.available or not language:
            return False
        lang = language.lower()
        if lang.split("-")[0] == self.cfg.target:
            return False
        return any(lang == t or lang.startswith(t + "-") for t in self.cfg.languages)

    def translate_article(
        self, title: str | None, content: str | None, language: str | None
    ) -> tuple[str | None, str | None]:
        """Return ``(title_fr, content_fr)``; ``None`` parts were not translated."""
        if not self.wants(language):
            return None, None
        title_fr = self.translate(title or "", language) if title else None
        body = (content or "")[: self.cfg.max_chars_per_article]
        content_fr = self.translate(body, language) if body else None
        if title_fr or content_fr:
            self.translated += 1
        return title_fr, content_fr

    def translate(self, text: str, language: str | None = None) -> str | None:
        """Translate ``text`` to the target language; ``None`` when not possible."""
        text = (text or "").strip()
        if not text or not self.available:
            return None
        if self.chars_used + len(text) > self.cfg.max_chars_per_run:
            if not self.budget_exhausted:
                logger.warning(
                    "translation budget reached (%d/%d chars) - remaining articles stored untranslated",
                    self.chars_used, self.cfg.max_chars_per_run,
                )
            self.budget_exhausted = True
            return None
        parts: list[str] = []
        for chunk in split_chunks(text):
            out = self._call(chunk, language)
            if out is None:
                self.failures += 1
                return None
            parts.append(out)
        self.chars_used += len(text)
        return "".join(parts).strip() or None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- internals --------------------------------------------------------------

    def _call(self, chunk: str, language: str | None) -> str | None:
        params = {"api-version": "3.0", "to": self.cfg.target}
        if language:
            params["from"] = "zh-Hans" if language.lower().startswith("zh") else language.lower()
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Content-Type": "application/json; charset=UTF-8",
        }
        if self._region:
            headers["Ocp-Apim-Subscription-Region"] = self._region
        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self._client.post(
                    AZURE_TRANSLATOR_URL, params=params, headers=headers, json=[{"Text": chunk}]
                )
            except httpx.HTTPError as exc:
                logger.warning("translator network error (attempt %d): %s", attempt + 1, exc)
                if attempt < self.cfg.max_retries:
                    self._sleep(2.0 * (2 ** attempt))
                    continue
                return None
            if resp.status_code == 200:
                try:
                    return resp.json()[0]["translations"][0]["text"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    logger.warning("translator: unexpected response: %s", exc)
                    return None
            if resp.status_code in (401, 403):
                # Bad key / region or quota exhausted: stop for the whole run.
                self.disabled_reason = f"http_{resp.status_code}: {resp.text[:200]}"
                logger.warning("translator disabled for this run - %s", self.disabled_reason)
                return None
            if resp.status_code in _RETRY_STATUS and attempt < self.cfg.max_retries:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * (2 ** attempt)
                logger.warning("translator http_%d - retry in %.0fs", resp.status_code, delay)
                self._sleep(delay)
                continue
            logger.warning("translator http_%d: %s", resp.status_code, resp.text[:200])
            return None
        return None


def build_translator(cfg: TranslationSection) -> AzureTranslator | None:
    """Translator for the run, or ``None`` when disabled / not configured."""
    if not cfg.enabled:
        return None
    if cfg.provider != "azure":
        logger.warning("translation provider %r not supported - translation disabled", cfg.provider)
        return None
    translator = AzureTranslator(cfg)
    if not translator.available:
        logger.info("translation skipped - %s (degraded)", translator.disabled_reason)
        return None
    return translator
