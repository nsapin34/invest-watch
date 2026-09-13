"""CRUD for sources, health counters and simple connectivity ping.

Source ``type`` follows the cascade (architecture §2/§4): ``rss`` > ``search`` >
``website`` > ``api``. The ping validation (PROJECT_STRUCTURE) updates the
``last_success_at`` / ``last_error_at`` / ``error_count`` columns used by the
orchestrator and the error manager (Steps 2/4/6).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from .db_manager import DatabaseManager, translate_integrity_error, utc_now_iso
from .exceptions import DuplicateEntityError, NotFoundError, RepositoryError

VALID_TYPES = ("rss", "search", "website", "api")

_UPDATABLE = frozenset({"name", "type", "url", "lang", "priority", "rate_limit_per_sec", "active"})


@dataclass
class Source:
    """One configured source (specs §9.1 ``source``)."""

    name: str
    type: str = "rss"
    url: str = ""
    lang: str = "*"
    priority: int = 5
    rate_limit_per_sec: float = 0.5
    active: bool = True
    id: int | None = None
    last_success_at: str | None = None
    last_error_at: str | None = None
    error_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.type not in VALID_TYPES:
            raise ValueError(f"invalid source type: {self.type!r} (expected {VALID_TYPES})")


class SourceRepository:
    """All database access for sources."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    # ── row mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Source:
        return Source(
            name=row["name"],
            type=row["type"],
            url=row["url"],
            lang=row["lang"],
            priority=row["priority"],
            rate_limit_per_sec=row["rate_limit_per_sec"],
            active=bool(row["active"]),
            id=row["id"],
            last_success_at=row["last_success_at"],
            last_error_at=row["last_error_at"],
            error_count=row["error_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── create / read ─────────────────────────────────────────────────────

    def add(self, source: Source) -> Source:
        """Insert a source. Raises :class:`DuplicateEntityError` on name conflict."""
        now = utc_now_iso()
        try:
            with self.db.transaction():
                cur = self.db.execute(
                    "INSERT INTO source (name, type, url, lang, priority, rate_limit_per_sec,"
                    " active, error_count, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,0,?,?)",
                    (
                        source.name.strip(),
                        source.type,
                        source.url,
                        source.lang,
                        source.priority,
                        source.rate_limit_per_sec,
                        int(bool(source.active)),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise translate_integrity_error(exc) from exc
        return replace(source, name=source.name.strip(), id=int(cur.lastrowid), created_at=now, updated_at=now)

    def get(self, source_id: int) -> Source | None:
        row = self.db.query_one("SELECT * FROM source WHERE id = ?", (source_id,))
        return self._from_row(row) if row else None

    def get_by_name(self, name: str) -> Source | None:
        row = self.db.query_one("SELECT * FROM source WHERE name = ?", (name.strip(),))
        return self._from_row(row) if row else None

    def get_by_ref(self, ref: str | int) -> Source:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            source = self.get(int(ref))
            if source:
                return source
        source = self.get_by_name(str(ref))
        if source is None:
            raise NotFoundError(f"source not found: {ref}")
        return source

    def list(self, *, include_inactive: bool = False) -> list[Source]:
        sql = "SELECT * FROM source"
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY priority, name COLLATE NOCASE"
        return [self._from_row(row) for row in self.db.query(sql)]

    def upsert_by_name(self, source: Source) -> Source:
        """Insert a source or update all its fields when the name already exists.

        Used by the config-seeding bootstrap (idempotent).
        """
        existing = self.get_by_name(source.name)
        if existing is None:
            return self.add(source)
        return self.update(
            existing.id,
            type=source.type,
            url=source.url,
            lang=source.lang,
            priority=source.priority,
            rate_limit_per_sec=source.rate_limit_per_sec,
            active=source.active,
        )

    # ── update / delete / health ──────────────────────────────────────────

    def update(self, source_id: int, **fields) -> Source:
        """Update allowed columns, bumping ``updated_at``."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise RepositoryError(f"unknown fields for source update: {sorted(unknown)}")
        if not fields:
            raise RepositoryError("no fields provided for source update")
        if "type" in fields and fields["type"] not in VALID_TYPES:
            raise RepositoryError(f"invalid source type: {fields['type']!r} (expected {VALID_TYPES})")
        if "active" in fields:
            fields = {**fields, "active": int(bool(fields["active"]))}
        sets = ", ".join(f"{column} = ?" for column in fields)
        params = [*fields.values(), utc_now_iso(), source_id]
        try:
            cur = self.db.execute(
                f"UPDATE source SET {sets}, updated_at = ? WHERE id = ?", params
            )
        except sqlite3.IntegrityError as exc:
            raise translate_integrity_error(exc) from exc
        if cur.rowcount == 0:
            raise NotFoundError(f"source #{source_id} not found")
        return self.get(source_id)

    def delete(self, source_id: int) -> bool:
        cur = self.db.execute("DELETE FROM source WHERE id = ?", (source_id,))
        return cur.rowcount > 0

    def record_success(self, source_id: int, *, at: str | None = None) -> None:
        """Mark a successful fetch (``last_success_at``). Counter untouched."""
        self.db.execute(
            "UPDATE source SET last_success_at = ?, updated_at = ? WHERE id = ?",
            (at or utc_now_iso(), utc_now_iso(), source_id),
        )

    def record_error(self, source_id: int, *, at: str | None = None) -> None:
        """Mark a failed fetch: set ``last_error_at`` and bump ``error_count``."""
        now = at or utc_now_iso()
        self.db.execute(
            "UPDATE source SET last_error_at = ?, error_count = COALESCE(error_count, 0) + 1,"
            " updated_at = ? WHERE id = ?",
            (now, utc_now_iso(), source_id),
        )

    def ping(self, source_id: int, *, timeout: float = 10.0) -> bool:
        """Fetch the source URL with ``httpx`` and refresh health columns.

        Any HTTP status < 400 counts as reachable. Network errors are recorded
        via :meth:`record_error` and return ``False`` (never raises).
        """
        source = self.get(source_id)
        if source is None:
            raise NotFoundError(f"source #{source_id} not found")
        try:
            import httpx  # local import: network dependency used on demand

            response = httpx.get(
                source.url,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "invest-watch/1.0"},
            )
            ok = 200 <= response.status_code < 400
        except Exception:  # noqa: BLE001 - degraded mode: never propagate network errors
            ok = False
        if ok:
            self.record_success(source_id)
        else:
            self.record_error(source_id)
        return ok