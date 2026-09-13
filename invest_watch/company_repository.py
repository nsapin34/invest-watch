"""CRUD for companies, their aliases and market links (architecture §4).

Companies are capped at 50 (ENF-10). Uniqueness of ``name`` is
case-insensitive via the schema collation; ``isin`` is unique when present.
All SQL goes through the shared :class:`~invest_watch.db_manager.DatabaseManager`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Iterable

from .db_manager import DatabaseManager, translate_integrity_error, utc_now_iso
from .exceptions import DuplicateEntityError, NotFoundError, RepositoryError

MAX_COMPANIES = 50  # ENF-10: controlled volume

_UPDATABLE = frozenset(
    {
        "name",
        "legal_name",
        "ticker",
        "isin",
        "country_code",
        "currency",
        "website",
        "primary_market_id",
        "summary",
        "active",
    }
)
_UNIQUE_COLUMNS = ("name", "isin")


@dataclass
class Company:
    """One tracked company (specs §9.1 ``company``)."""

    name: str
    legal_name: str | None = None
    ticker: str | None = None
    isin: str | None = None
    country_code: str | None = None
    currency: str | None = None
    website: str | None = None
    primary_market_id: int | None = None
    summary: str | None = None
    active: bool = True
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class Alias:
    """One search alias row (``search_alias``, entity_type 'company')."""

    term: str
    lang: str = "*"
    priority: int = 5
    active: bool = True
    id: int | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        self.term = self.term.strip()


class CompanyRepository:
    """All database access for companies, aliases and company↔market links."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    # ── row mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Company:
        return Company(
            name=row["name"],
            legal_name=row["legal_name"],
            ticker=row["ticker"],
            isin=row["isin"],
            country_code=row["country_code"],
            currency=row["currency"],
            website=row["website"],
            primary_market_id=row["primary_market_id"],
            summary=row["summary"],
            active=bool(row["active"]),
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _alias_from_row(row: sqlite3.Row) -> Alias:
        return Alias(
            term=row["term"],
            lang=row["lang"],
            priority=row["priority"],
            active=bool(row["active"]),
            id=row["id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _check_cap() -> None:
        raise RepositoryError(
            f"company limit reached: maximum {MAX_COMPANIES} companies (ENF-10)"
        )

    # ── create / read ─────────────────────────────────────────────────────

    def add(
        self,
        company: Company,
        *,
        markets: Iterable[tuple[int, float]] = (),
        aliases: Iterable[str] = (),
    ) -> Company:
        """Insert a company plus optional market links and alias terms.

        Raises :class:`DuplicateEntityError` on name/ISIN conflict and
        enforces the 50-company cap (ENF-10).
        """
        if self.count() >= MAX_COMPANIES:
            self._check_cap()
        now = utc_now_iso()
        try:
            with self.db.transaction():
                cur = self.db.execute(
                    "INSERT INTO company (name, legal_name, ticker, isin, country_code,"
                    " currency, website, primary_market_id, summary, active, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        company.name.strip(),
                        company.legal_name,
                        company.ticker,
                        company.isin,
                        company.country_code,
                        company.currency,
                        company.website,
                        company.primary_market_id,
                        company.summary,
                        int(bool(company.active)),
                        now,
                        now,
                    ),
                )
                company_id = int(cur.lastrowid)
                for market_id, weight in markets:
                    self.db.execute(
                        "INSERT INTO company_market (company_id, market_id, weight)"
                        " VALUES (?,?,?)",
                        (company_id, market_id, weight),
                    )
                for term in aliases:
                    self._insert_alias(company_id, term)
        except sqlite3.IntegrityError as exc:
            raise translate_integrity_error(exc) from exc
        return replace(company, id=company_id, created_at=now, updated_at=now)

    def get(self, company_id: int) -> Company | None:
        row = self.db.query_one("SELECT * FROM company WHERE id = ?", (company_id,))
        return self._from_row(row) if row else None

    def get_by_name(self, name: str) -> Company | None:
        row = self.db.query_one(
            "SELECT * FROM company WHERE name = ? COLLATE NOCASE", (name.strip(),)
        )
        return self._from_row(row) if row else None

    def get_by_ticker(self, ticker: str) -> Company | None:
        row = self.db.query_one(
            "SELECT * FROM company WHERE ticker = ? COLLATE NOCASE", (ticker.strip(),)
        )
        return self._from_row(row) if row else None

    def get_by_isin(self, isin: str) -> Company | None:
        row = self.db.query_one("SELECT * FROM company WHERE isin = ?", (isin.strip(),))
        return self._from_row(row) if row else None

    def list(self, *, include_inactive: bool = False) -> list[Company]:
        sql = "SELECT * FROM company"
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY name COLLATE NOCASE"
        return [self._from_row(row) for row in self.db.query(sql)]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM company")
        return int(row["n"])

    def search(self, text: str) -> list[Company]:
        """Search vectors: name, legal name, ticker, ISIN, website (architecture §4)."""
        pattern = f"%{text.strip()}%"
        rows = self.db.query(
            "SELECT * FROM company"
            " WHERE name COLLATE NOCASE LIKE ? ESCAPE '\\'"
            " OR legal_name COLLATE NOCASE LIKE ? ESCAPE '\\'"
            " OR ticker COLLATE NOCASE LIKE ? ESCAPE '\\'"
            " OR isin LIKE ? ESCAPE '\\'"
            " OR website COLLATE NOCASE LIKE ? ESCAPE '\\'"
            " ORDER BY name COLLATE NOCASE",
            (pattern, pattern, pattern, pattern, pattern),
        )
        return [self._from_row(row) for row in rows]

    # ── update / delete ───────────────────────────────────────────────────

    def update(self, company_id: int, **fields) -> Company:
        """Update allowed columns, bumping ``updated_at``.

        Raises :class:`DuplicateEntityError` when the change violates a
        uniqueness constraint and :class:`NotFoundError` for unknown id.
        """
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise RepositoryError(f"unknown fields for company update: {sorted(unknown)}")
        if not fields:
            raise RepositoryError("no fields provided for company update")
        if "active" in fields:
            fields = {**fields, "active": int(bool(fields["active"]))}
        sets = ", ".join(f"{column} = ?" for column in fields)
        params = [*fields.values(), utc_now_iso(), company_id]
        try:
            cur = self.db.execute(
                f"UPDATE company SET {sets}, updated_at = ? WHERE id = ?", params
            )
        except sqlite3.IntegrityError as exc:
            raise translate_integrity_error(exc) from exc
        if cur.rowcount == 0:
            raise NotFoundError(f"company #{company_id} not found")
        return self.get(company_id)

    def delete(self, company_id: int) -> bool:
        """Hard-delete a company (junctions and aliases cascade)."""
        cur = self.db.execute("DELETE FROM company WHERE id = ?", (company_id,))
        return cur.rowcount > 0

    def get_by_ref(self, ref: str | int) -> Company:
        """Resolve a CLI-style reference: integer id, else case-insensitive name."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            company = self.get(int(ref))
            if company:
                return company
        company = self.get_by_name(str(ref))
        if company is None:
            raise NotFoundError(f"company not found: {ref}")
        return company

    # ── aliases ───────────────────────────────────────────────────────────

    def _insert_alias(self, company_id: int, term: str, lang: str = "*", priority: int = 5) -> None:
        self.db.execute(
            "INSERT INTO search_alias (entity_type, company_id, term, lang, priority, active, created_at)"
            " VALUES ('company', ?, ?, ?, ?, 1, ?)",
            (company_id, term.strip(), lang, priority, utc_now_iso()),
        )

    def add_alias(self, company_id: int, term: str, lang: str = "*", priority: int = 5) -> Alias:
        """Add a single alias (idempotent — returns the existing row if present)."""
        if self.get(company_id) is None:
            raise NotFoundError(f"company #{company_id} not found")
        try:
            self._insert_alias(company_id, term, lang, priority)
        except sqlite3.IntegrityError:
            pass  # already present -> fetch below
        row = self.db.query_one(
            "SELECT * FROM search_alias WHERE entity_type = 'company' AND company_id = ?"
            " AND term = ? COLLATE NOCASE",
            (company_id, term.strip()),
        )
        return self._alias_from_row(row)

    def set_aliases(
        self, company_id: int, terms: Iterable[str], lang: str = "*", priority: int = 5
    ) -> list[Alias]:
        """Replace all company aliases with the given terms (idempotent)."""
        if self.get(company_id) is None:
            raise NotFoundError(f"company #{company_id} not found")
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM search_alias WHERE entity_type = 'company' AND company_id = ?",
                (company_id,),
            )
            inserted: list[Alias] = []
            for term in terms:
                cleaned = str(term).strip()
                if cleaned and all(cleaned.casefold() != alias.term.casefold() for alias in inserted):
                    self._insert_alias(company_id, cleaned, lang, priority)
                    inserted.append(Alias(term=cleaned, lang=lang, priority=priority, active=True))
        return inserted

    def remove_alias(self, company_id: int, term: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM search_alias WHERE entity_type = 'company' AND company_id = ?"
            " AND term = ? COLLATE NOCASE",
            (company_id, term.strip()),
        )
        return cur.rowcount > 0

    def aliases_for(self, company_id: int) -> list[Alias]:
        rows = self.db.query(
            "SELECT * FROM search_alias WHERE entity_type = 'company' AND company_id = ?"
            " ORDER BY rowid",
            (company_id,),
        )
        return [self._alias_from_row(row) for row in rows]

    # ── company <-> market links ──────────────────────────────────────────

    def attach_market(self, company_id: int, market_id: int, weight: float = 1.0) -> None:
        """Link a company to a market (upsert — idempotent)."""
        if self.get(company_id) is None:
            raise NotFoundError(f"company #{company_id} not found")
        self.db.execute(
            "INSERT INTO company_market (company_id, market_id, weight) VALUES (?,?,?)"
            " ON CONFLICT(company_id, market_id) DO UPDATE SET weight = excluded.weight",
            (company_id, market_id, weight),
        )

    def detach_market(self, company_id: int, market_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM company_market WHERE company_id = ? AND market_id = ?",
            (company_id, market_id),
        )
        return cur.rowcount > 0

    def markets_for(self, company_id: int) -> list[tuple["Market", float]]:
        """Return (Market, weight) pairs linked to the company, ordered by name."""
        rows = self.db.query(
            "SELECT m.*, cm.weight AS w FROM company_market cm"
            " JOIN market m ON m.id = cm.market_id"
            " WHERE cm.company_id = ? ORDER BY m.name COLLATE NOCASE",
            (company_id,),
        )
        from .market_repository import Market  # local import avoids a cycle

        return [(Market._from_row(row), float(row["w"])) for row in rows]