"""CRUD for markets / sectors / industries with a 3-level hierarchy.

Taxonomy (specs §Q3): ``market`` (region/exchange) > ``sector`` (GICS level) >
``industry`` (GICS level). A child's ``type`` must rank strictly below its
parent's: a sector cannot contain another sector, an industry cannot contain
a market, etc. Cycles are rejected when reparenting.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Iterable

from .db_manager import DatabaseManager, translate_integrity_error, utc_now_iso
from .exceptions import DuplicateEntityError, NotFoundError, RepositoryError

TYPE_LEVELS = {"market": 0, "sector": 1, "industry": 2}

_UPDATABLE = frozenset({"name", "code", "type", "parent_id", "active"})


@dataclass
class Market:
    """One market / sector / industry node (specs §9.1 ``market``)."""

    name: str
    type: str = "market"
    code: str | None = None
    parent_id: int | None = None
    active: bool = True
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.type not in TYPE_LEVELS:
            raise ValueError(f"invalid market type: {self.type!r} (expected {sorted(TYPE_LEVELS)})")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> "Market":
        return Market(
            name=row["name"],
            type=row["type"],
            code=row["code"],
            parent_id=row["parent_id"],
            active=bool(row["active"]),
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class MarketNode:
    """A node of the hierarchical market tree (used by ``MarketRepository.tree``)."""

    market: Market
    children: list["MarketNode"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []

    def flatten(self) -> list[Market]:
        """Depth-first list of the subtree (self first)."""
        result: list[Market] = [self.market]
        for child in self.children:
            result.extend(child.flatten())
        return result


def _scroll_to(nodes: Iterable[MarketNode], market_id: int) -> MarketNode | None:
    for node in nodes:
        if node.market.id == market_id:
            return node
        found = _scroll_to(node.children, market_id)
        if found:
            return found
    return None


class MarketRepository:
    """All database access for the market taxonomy."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    # ── row mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Market:
        return Market._from_row(row)

    @staticmethod
    def _validate_parent_type(parent_type: str, child_type: str) -> None:
        if TYPE_LEVELS[parent_type] >= TYPE_LEVELS[child_type]:
            raise RepositoryError(
                f"invalid hierarchy: a '{child_type}' cannot be nested under a '{parent_type}'"
            )

    # ── create / read ─────────────────────────────────────────────────────

    def add(
        self,
        name: str,
        type: str,
        *,
        code: str | None = None,
        parent_id: int | None = None,
        active: bool = True,
    ) -> Market:
        """Insert a market node, validating the type and the parent level."""
        if type not in TYPE_LEVELS:
            raise RepositoryError(f"invalid market type: {type!r} (expected {sorted(TYPE_LEVELS)})")
        if parent_id is not None:
            parent = self.get(parent_id)
            if parent is None:
                raise NotFoundError(f"parent market #{parent_id} not found")
            self._validate_parent_type(parent.type, type)
        now = utc_now_iso()
        try:
            with self.db.transaction():
                cur = self.db.execute(
                    "INSERT INTO market (name, type, code, parent_id, active, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (name.strip(), type, code, parent_id, int(bool(active)), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise translate_integrity_error(exc) from exc
        market_id = int(cur.lastrowid)
        return Market(
            name=name.strip(),
            type=type,
            code=code,
            parent_id=parent_id,
            active=active,
            id=market_id,
            created_at=now,
            updated_at=now,
        )

    def get(self, market_id: int) -> Market | None:
        row = self.db.query_one("SELECT * FROM market WHERE id = ?", (market_id,))
        return self._from_row(row) if row else None

    def get_by_name(self, name: str) -> Market | None:
        row = self.db.query_one("SELECT * FROM market WHERE name = ?", (name.strip(),))
        if row is None:  # friendly fallback: case-insensitive
            row = self.db.query_one(
                "SELECT * FROM market WHERE name = ? COLLATE NOCASE", (name.strip(),)
            )
        return self._from_row(row) if row else None

    def get_by_code(self, code: str) -> Market | None:
        row = self.db.query_one("SELECT * FROM market WHERE code = ?", (code.strip(),))
        return self._from_row(row) if row else None

    def get_by_ref(self, ref: str | int) -> Market:
        """Resolve a CLI-style reference: integer id, else name/fallback NOCASE."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            market = self.get(int(ref))
            if market:
                return market
        market = self.get_by_name(str(ref))
        if market is None:
            raise NotFoundError(f"market not found: {ref}")
        return market

    def list(
        self,
        *,
        include_inactive: bool = False,
        type: str | None = None,
    ) -> list[Market]:
        clauses: list[str] = []
        params: list = []
        if not include_inactive:
            clauses.append("active = 1")
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM market{where} ORDER BY type, name COLLATE NOCASE", params
        )
        return [self._from_row(row) for row in rows]

    def children(self, market_id: int) -> list[Market]:
        rows = self.db.query(
            "SELECT * FROM market WHERE parent_id = ? ORDER BY name COLLATE NOCASE", (market_id,)
        )
        return [self._from_row(row) for row in rows]

    def ancestors(self, market_id: int) -> list[Market]:
        """Immediate parent first, ending at the root of the hierarchy."""
        result: list[Market] = []
        seen: set[int] = set()
        current = self.get(market_id)
        while current is not None and current.parent_id is not None and current.parent_id not in seen:
            seen.add(current.parent_id)
            parent = self.get(current.parent_id)
            if parent is None:
                break
            result.append(parent)
            current = parent
        return result

    def tree(self, *, include_inactive: bool = False) -> list[MarketNode]:
        """Build the forest of market nodes rooted at nodes without parent (or orphans)."""
        nodes = [
            MarketNode(market=market)
            for market in self.list(include_inactive=include_inactive)
        ]
        by_id = {node.market.id: node for node in nodes if node.market.id is not None}
        roots: list[MarketNode] = []
        for node in nodes:
            parent = by_id.get(node.market.parent_id)
            if parent is not None:
                parent.children.append(node)
            else:
                roots.append(node)
        return roots

    # ── update / delete ───────────────────────────────────────────────────

    def update(self, market_id: int, **fields) -> Market:
        """Update a market node, re-validating hierarchy and cycles."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise RepositoryError(f"unknown fields for market update: {sorted(unknown)}")
        if not fields:
            raise RepositoryError("no fields provided for market update")
        current = self.get(market_id)
        if current is None:
            raise NotFoundError(f"market #{market_id} not found")

        new_type = fields.get("type", current.type)
        if new_type not in TYPE_LEVELS:
            raise RepositoryError(
                f"invalid market type: {new_type!r} (expected {sorted(TYPE_LEVELS)})"
            )
        new_parent = fields["parent_id"] if "parent_id" in fields else current.parent_id
        if "parent_id" in fields and new_parent is not None:
            if int(new_parent) == market_id:
                raise RepositoryError("a market cannot be its own parent")
            parent = self.get(int(new_parent))
            if parent is None:
                raise NotFoundError(f"parent market #{new_parent} not found")
            self._validate_parent_type(parent.type, new_type)
            if self._is_descendant(market_id, int(new_parent)):
                raise RepositoryError(
                    f"cannot re-parent market #{market_id} under its own descendant #{new_parent}"
                )

        if "active" in fields:
            fields = {**fields, "active": int(bool(fields["active"]))}
        if "code" in fields and fields["code"] == "":
            fields = {**fields, "code": None}
        sets = ", ".join(f"{column} = ?" for column in fields)
        params = [*fields.values(), utc_now_iso(), market_id]
        try:
            self.db.execute(
                f"UPDATE market SET {sets}, updated_at = ? WHERE id = ?", params
            )
        except sqlite3.IntegrityError as exc:
            raise translate_integrity_error(exc) from exc
        return self.get(market_id)

    def delete(self, market_id: int, *, force: bool = False) -> bool:
        """Delete a market. Without ``force`` a market that still has children
        is refused (children would silently become root nodes)."""
        if self.get(market_id) is None:
            raise NotFoundError(f"market #{market_id} not found")
        if not force and self.children(market_id):
            raise RepositoryError(
                f"market #{market_id} still has children; pass force=True to detach them"
            )
        cur = self.db.execute("DELETE FROM market WHERE id = ?", (market_id,))
        return cur.rowcount > 0

    def _is_descendant(self, market_id: int, candidate: int) -> bool:
        """True when ``candidate`` is reachable as a descendant of ``market_id``."""
        for child in self.children(market_id):
            if child.id == candidate or (child.id is not None and self._is_descendant(child.id, candidate)):
                return True
        return False


def _print_tree(nodes: Iterable[MarketNode], indent: int = 0) -> str:
    """Render a market forest as an indented text tree (CLI display helper)."""
    lines: list[str] = []
    for node in nodes:
        market = node.market
        label = f"{market.name} ({market.type})"
        if market.code:
            label += f" [{market.code}]"
        lines.append("  " * indent + label)
        lines.append(_print_tree(node.children, indent + 1))
    return "\n".join(line for line in lines if line)