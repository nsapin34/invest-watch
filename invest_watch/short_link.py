"""Secure local short-link generation, repository and Markdown rendering.

Implements specifications §4.5, §13.3 (F17, AC7) and architecture §4 / §8 (Step 3).
Features:
- Secure random short code generation (6-8 characters, `secrets.token_urlsafe`).
- Deterministic or unique random code creation with collision resistance.
- Database persistence in `short_link` table linked to `article_id` / `url_original`.
- Markdown link rendering: `format_markdown_link(code, url)` -> `[code](url)`.
- Markdown inline short code format: `format_inline_code(code)` -> `[code]`.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass

from .db_manager import DatabaseManager, translate_integrity_error, utc_now_iso
from .exceptions import DuplicateEntityError, NotFoundError, RepositoryError

# Valid characters in URL-safe base64 without padding (alphanumeric, -, _)
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,8}$")
DEFAULT_CODE_LENGTH = 6
MAX_COLLISION_RETRIES = 10


def generate_short_code(length: int = DEFAULT_CODE_LENGTH) -> str:
    """Generate a cryptographically secure random short code of `length` chars (6-8).

    Uses `secrets.token_urlsafe`.
    """
    if length < 6 or length > 8:
        raise ValueError(f"short code length must be between 6 and 8 characters (got {length})")
    # token_urlsafe(nbytes) generates ~ 4/3 * nbytes characters
    # We generate slightly more bytes and slice to exact length
    code = secrets.token_urlsafe(length + 2)[:length]
    # In case token_urlsafe gave characters outside standard urlsafe set (should never happen),
    # replace any unexpected chars.
    code = re.sub(r"[^A-Za-z0-9_-]", "x", code)
    return code


def format_markdown_link(short_code: str, url: str) -> str:
    """Render a short code and target URL as Markdown link: `[code](url)`."""
    clean_code = short_code.strip()
    clean_url = url.strip()
    return f"[{clean_code}]({clean_url})"


def format_inline_code(short_code: str) -> str:
    """Render a short code badge format: `[code]`."""
    return f"[{short_code.strip()}]"


@dataclass(frozen=True)
class ShortLink:
    """Representation of a persisted short_link record."""

    short_code: str
    url_original: str
    article_id: int | None = None
    provider: str = "local"
    external_url: str | None = None
    created_at: str | None = None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ShortLink:
        return ShortLink(
            short_code=row["short_code"],
            url_original=row["url_original"],
            article_id=row["article_id"],
            provider=row["provider"],
            external_url=row["external_url"],
            created_at=row["created_at"],
        )


class ShortLinkRepository:
    """CRUD & resolution for the `short_link` table."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def get_by_code(self, short_code: str) -> ShortLink | None:
        """Find a ShortLink by its short_code."""
        row = self.db.query_one(
            "SELECT * FROM short_link WHERE short_code = ?",
            (short_code.strip(),),
        )
        return ShortLink._from_row(row) if row is not None else None

    def get_by_url(self, url: str) -> ShortLink | None:
        """Find a ShortLink by its original URL."""
        row = self.db.query_one(
            "SELECT * FROM short_link WHERE url_original = ?",
            (url.strip(),),
        )
        return ShortLink._from_row(row) if row is not None else None

    def get_by_article_id(self, article_id: int) -> ShortLink | None:
        """Find a ShortLink linked to a given article ID."""
        row = self.db.query_one(
            "SELECT * FROM short_link WHERE article_id = ?",
            (article_id,),
        )
        return ShortLink._from_row(row) if row is not None else None

    def create(
        self,
        url_original: str,
        *,
        article_id: int | None = None,
        code_length: int = DEFAULT_CODE_LENGTH,
        provider: str = "local",
        external_url: str | None = None,
        custom_code: str | None = None,
    ) -> ShortLink:
        """Create and persist a new ShortLink.

        If a ShortLink already exists for `url_original`, returns the existing record
        (and attaches article_id if it wasn't attached).
        """
        clean_url = url_original.strip()
        if not clean_url:
            raise ValueError("url_original cannot be empty")

        existing = self.get_by_url(clean_url)
        if existing is not None:
            if article_id is not None and existing.article_id != article_id:
                self.db.execute(
                    "UPDATE short_link SET article_id = ? WHERE short_code = ?",
                    (article_id, existing.short_code),
                )
                return self.get_by_code(existing.short_code)  # type: ignore[return-value]
            return existing

        now = utc_now_iso()

        if custom_code:
            code = custom_code.strip()
            if not (6 <= len(code) <= 8):
                raise ValueError("custom_code length must be between 6 and 8 characters")
            try:
                self.db.execute(
                    "INSERT INTO short_link (short_code, url_original, article_id, provider, external_url, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (code, clean_url, article_id, provider, external_url, now),
                )
            except sqlite3.IntegrityError as exc:
                raise translate_integrity_error(exc) from exc
            return ShortLink(
                short_code=code,
                url_original=clean_url,
                article_id=article_id,
                provider=provider,
                external_url=external_url,
                created_at=now,
            )

        for _ in range(MAX_COLLISION_RETRIES):
            code = generate_short_code(code_length)
            try:
                self.db.execute(
                    "INSERT INTO short_link (short_code, url_original, article_id, provider, external_url, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (code, clean_url, article_id, provider, external_url, now),
                )
                return ShortLink(
                    short_code=code,
                    url_original=clean_url,
                    article_id=article_id,
                    provider=provider,
                    external_url=external_url,
                    created_at=now,
                )
            except sqlite3.IntegrityError:
                # Collision on short_code or url_original raced
                raced = self.get_by_url(clean_url)
                if raced is not None:
                    return raced
                continue

        raise RepositoryError(f"failed to generate unique short_code after {MAX_COLLISION_RETRIES} attempts")

    def resolve(self, short_code: str) -> str:
        """Resolve a short_code to its original URL. Raises NotFoundError if unknown."""
        link = self.get_by_code(short_code)
        if link is None:
            raise NotFoundError(f"short code '{short_code}' not found")
        return link.url_original

    def render_markdown(self, short_code: str) -> str:
        """Lookup short_code and format as Markdown link `[code](url)`."""
        url = self.resolve(short_code)
        return format_markdown_link(short_code, url)
