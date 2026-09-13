"""SQLite connection management and versioned migrations.

Single SQLite file ``data/invest.db`` in WAL mode (specs §4.1, architecture §4).
Every module writes through :class:`DatabaseManager`; migrations live in the
``migrations/`` directory as ``vNNN_name.sql`` scripts and are tracked in
``app_meta.db_version``.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .exceptions import DatabaseError, DuplicateEntityError, MigrationError

_MIGRATION_RE = re.compile(r"^v(\d+)_.*\.sql$")
# Relative to the package parent (repository root, see PROJECT_STRUCTURE).
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def utc_now_iso() -> str:
    """Current UTC time as sortable ISO-8601 text (``YYYY-MM-DDTHH:MM:SSZ``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def translate_integrity_error(exc: sqlite3.IntegrityError) -> DatabaseError:
    """Map an SQLite constraint error to the domain exception it represents."""
    message = str(exc)
    if "UNIQUE constraint failed" in message:
        return DuplicateEntityError(message)
    return DatabaseError(message)


class DatabaseManager:
    """Owns one WAL SQLite connection and applies versioned migrations."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        timeout: float = 30.0,
        migrations_dir: str | Path | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.timeout = timeout
        self.migrations_dir = Path(migrations_dir) if migrations_dir else _DEFAULT_MIGRATIONS_DIR
        self._conn: sqlite3.Connection | None = None

    # ── connection lifecycle ──────────────────────────────────────────────

    @property
    def is_file_db(self) -> bool:
        return self.db_path != ":memory:"

    @property
    def connection(self) -> sqlite3.Connection:
        """Lazily open the connection with WAL / FK pragmas applied."""
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> sqlite3.Connection:
        if self.is_file_db:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if self.is_file_db:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── low-level helpers ─────────────────────────────────────────────────

    def execute(self, sql: str, params: tuple | list | None = None) -> sqlite3.Cursor:
        """Execute a statement on the shared connection (autocommit mode)."""
        return self.connection.execute(sql, params or ())

    def query(self, sql: str, params: tuple | list | None = None) -> list[sqlite3.Row]:
        """Run a SELECT and return every row."""
        return self.connection.execute(sql, params or ()).fetchall()

    def query_one(self, sql: str, params: tuple | list | None = None) -> sqlite3.Row | None:
        """Run a SELECT and return the first row (or ``None``)."""
        return self.connection.execute(sql, params or ()).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Exclusive transaction: commit on success, rollback on any error."""
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # ── app_meta key/value store ──────────────────────────────────────────

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        try:
            row = self.query_one("SELECT value FROM app_meta WHERE key = ?", (key,))
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return default  # brand-new database, schema not migrated yet
            raise
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def schema_version(self) -> int:
        """Current schema version read from ``app_meta`` (0 when unset)."""
        raw = self.get_meta("db_version", "0")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    # ── migrations ────────────────────────────────────────────────────────

    def _discover_migrations(self) -> list[tuple[int, Path]]:
        if not self.migrations_dir.is_dir():
            raise MigrationError(f"migrations directory not found: {self.migrations_dir}")
        found: dict[int, Path] = {}
        for path in sorted(self.migrations_dir.glob("v*.sql")):
            match = _MIGRATION_RE.match(path.name)
            if not match:
                continue
            version = int(match.group(1))
            if version in found:
                raise MigrationError(
                    f"duplicate migration version {version} ({found[version].name} vs {path.name})"
                )
            found[version] = path
        return sorted(found.items())

    def latest_migration_version(self) -> int:
        """Highest version available in the migrations directory (0 if none)."""
        versioned = self._discover_migrations()
        return versioned[-1][0] if versioned else 0

    def apply_migrations(self, *, up_to: int | None = None) -> list[str]:
        """Apply pending migrations in ascending order.

        Each migration runs in its own transaction together with the
        ``app_meta.db_version`` bump, so a failure leaves the schema at its
        previous version (idempotent — re-running applies nothing).
        Returns the list of applied file names.
        """
        conn = self.connection
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
        current = self.schema_version
        applied: list[str] = []
        for version, path in self._discover_migrations():
            if version <= current:
                continue
            if up_to is not None and version > up_to:
                continue
            sql_text = path.read_text(encoding="utf-8-sig")
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{sql_text}\n"
                f"INSERT INTO app_meta (key, value) VALUES ('db_version', '{version}') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise MigrationError(
                    f"migration {path.name} failed (schema left at v{current}): {exc}"
                ) from exc
            applied.append(path.name)
            current = version
        return applied