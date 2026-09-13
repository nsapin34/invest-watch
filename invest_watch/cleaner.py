"""HTML → main text (trafilatura), excerpt, normalisation, content hash.

Paywalls are never bypassed: when the main text is missing or a paywall
marker is present, only OpenGraph / meta description metadata is kept
(specs §6.7). Dynamic pages without server-rendered text follow the same
degraded path (no headless browser in V1).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

EXCERPT_CHARS = 400
_WS_RE = re.compile(r"\s+")
_PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscribe to read",
    "become a subscriber",
    "pour lire la suite",
    "article réservé aux abonnés",
    "réservé aux abonnés",
    "paywall",
    "metered paywall",
    "this article is for subscribers",
)


@dataclass
class CleanedDocument:
    """Extracted article body plus traceability fields."""

    text: str
    excerpt: str
    word_count: int
    content_hash: str | None
    title: str | None = None
    author: str | None = None
    description: str | None = None
    paywall: bool = False
    metadata_only: bool = False


def normalise_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").replace("\xa0", " ")).strip()


def content_hash(text: str) -> str:
    normalised = normalise_whitespace(text).lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def title_hash(title: str) -> str:
    return content_hash(title)


def excerpt_of(text: str, limit: int = EXCERPT_CHARS) -> str:
    compact = normalise_whitespace(text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _meta_description(html: str) -> tuple[str | None, str | None, str | None]:
    soup = BeautifulSoup(html, "lxml")
    title = None
    if soup.title and soup.title.string:
        title = normalise_whitespace(soup.title.string)
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = normalise_whitespace(str(og_title["content"]))
    author = None
    author_tag = soup.find("meta", attrs={"name": "author"}) or soup.find("meta", property="article:author")
    if author_tag and author_tag.get("content"):
        author = normalise_whitespace(str(author_tag["content"]))
    description = None
    for attrs in ({"property": "og:description"}, {"name": "description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            description = normalise_whitespace(str(tag["content"]))
            break
    return title, author, description


def _looks_like_paywall(html: str, text: str) -> bool:
    blob = f"{html} {text}".lower()
    return any(marker in blob for marker in _PAYWALL_MARKERS)


def clean_html(
    html: str,
    *,
    min_content_chars: int = 200,
    url: str | None = None,
) -> CleanedDocument:
    """Extract main text; fall back to metadata when extraction is too thin."""
    title, author, description = _meta_description(html)
    extracted = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        ) or ""
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta is not None:
            title = title or (meta.title or None)
            author = author or (meta.author or None)
            if not description and meta.description:
                description = normalise_whitespace(meta.description)
    except Exception as exc:  # noqa: BLE001 — degraded: keep metadata
        logger.warning("trafilatura failed: %s", exc)

    text = normalise_whitespace(extracted)
    paywall = _looks_like_paywall(html, text)
    too_short = len(text) < min_content_chars
    if paywall or too_short:
        fallback = description or text
        fallback = normalise_whitespace(fallback)
        hashed = content_hash(fallback) if fallback else None
        return CleanedDocument(
            text=fallback,
            excerpt=excerpt_of(fallback),
            word_count=len(fallback.split()) if fallback else 0,
            content_hash=hashed,
            title=title,
            author=author,
            description=description,
            paywall=paywall,
            metadata_only=True,
        )
    return CleanedDocument(
        text=text,
        excerpt=excerpt_of(text),
        word_count=len(text.split()),
        content_hash=content_hash(text),
        title=title,
        author=author,
        description=description,
        paywall=False,
        metadata_only=False,
    )
