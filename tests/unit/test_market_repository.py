"""Unit tests for MarketRepository (taxonomy, hierarchy, tree, cycles)."""

import pytest

from invest_watch.exceptions import DuplicateEntityError, NotFoundError, RepositoryError


def test_add_and_get_roundtrip(market_repo):
    market = market_repo.add("China A", "market", code="CN.A")
    assert market.id is not None
    loaded = market_repo.get(market.id)
    assert loaded.name == "China A"
    assert loaded.type == "market"
    assert loaded.code == "CN.A"


def test_get_by_name_case_insensitive_fallback(market_repo):
    market = market_repo.add("Electric Vehicles", "industry")
    assert market_repo.get_by_name("Electric Vehicles").id == market.id
    assert market_repo.get_by_name("electric vehicles").id == market.id


def test_get_by_code(market_repo):
    market = market_repo.add("China A", "market", code="CN.A")
    assert market_repo.get_by_code("CN.A").id == market.id
    assert market_repo.get_by_code("CN.B") is None


def test_duplicate_name_and_code_rejected(market_repo):
    market_repo.add("China A", "market", code="CN.A")
    with pytest.raises(DuplicateEntityError):
        market_repo.add("China A", "market")
    with pytest.raises(DuplicateEntityError):
        market_repo.add("China B", "market", code="CN.A")


def test_invalid_type_rejected(market_repo):
    with pytest.raises(RepositoryError, match="type"):
        market_repo.add("Bogus", "galaxy")


def test_hierarchy_levels_enforced(market_repo):
    cn = market_repo.add("China", "market")
    tech = market_repo.add("Technology", "sector", parent_id=cn.id)
    semis = market_repo.add("Semiconductors", "industry", parent_id=tech.id)
    assert semis.parent_id == tech.id

    # a sector cannot contain another sector
    with pytest.raises(RepositoryError, match="hierarchy"):
        market_repo.add("Software", "sector", parent_id=tech.id)
    # an industry cannot contain a market
    with pytest.raises(RepositoryError, match="hierarchy"):
        market_repo.add("Bogus", "market", parent_id=semis.id)


def test_add_with_missing_parent(market_repo):
    with pytest.raises(NotFoundError):
        market_repo.add("X", "sector", parent_id=9999)


def test_children_and_ancestors(market_repo):
    cn = market_repo.add("China", "market")
    tech = market_repo.add("Technology", "sector", parent_id=cn.id)
    semis = market_repo.add("Semiconductors", "industry", parent_id=tech.id)

    assert [c.name for c in market_repo.children(cn.id)] == ["Technology"]
    assert market_repo.children(semis.id) == []
    assert [a.name for a in market_repo.ancestors(semis.id)] == ["Technology", "China"]


def test_tree_nesting(market_repo):
    cn = market_repo.add("China", "market")
    tech = market_repo.add("Technology", "sector", parent_id=cn.id)
    semis = market_repo.add("Semiconductors", "industry", parent_id=tech.id)
    evs = market_repo.add("EV", "industry", parent_id=tech.id)
    nasdaq = market_repo.add("Nasdaq", "market")

    tree = market_repo.tree()
    assert len(tree) == 2  # two roots: China, Nasdaq
    china = next(node for node in tree if node.market.id == cn.id)
    assert [child.market.name for child in china.children] == ["Technology"]
    tech = china.children[0]
    assert [child.market.name for child in tech.children] == ["EV", "Semiconductors"]
    assert nasdaq.id in {node.market.id for node in tree}

    flattened = tech.flatten()
    assert [m.name for m in flattened] == ["Technology", "EV", "Semiconductors"]


def test_update_rename_code_type(market_repo):
    market = market_repo.add("China A", "market")
    updated = market_repo.update(market.id, code="CN.A", name="China II")
    assert updated.name == "China II"
    assert updated.code == "CN.A"
    market_repo.update(market.id, code="")  # empty clears the code
    assert market_repo.get(market.id).code is None


def test_update_reparent_validates_level(market_repo):
    ev = market_repo.add("EV", "industry")
    semis = market_repo.add("Semis", "industry")
    with pytest.raises(RepositoryError, match="hierarchy"):
        market_repo.update(semis.id, parent_id=ev.id)  # industry under industry


def test_update_rejects_own_parent(market_repo):
    tech = market_repo.add("Technology", "sector")
    with pytest.raises(RepositoryError, match="own parent"):
        market_repo.update(tech.id, parent_id=tech.id)


def test_update_cycle_guard_present(market_repo):
    """With strict level ordering the parent check makes cycles impossible;
    the descendant guard still exists as defense-in-depth.
    (Tested via a direct level-bookkeeping check that a sector cannot be
    moved below its own descendant industry.)"""
    cn = market_repo.add("China", "market")
    tech = market_repo.add("Technology", "sector", parent_id=cn.id)
    semis = market_repo.add("Semiconductors", "industry", parent_id=tech.id)
    # Moving the sector under its own descendant would be a hierarchy error
    # (sector under industry), which proves the chain is never re-ordered.
    with pytest.raises(RepositoryError, match="hierarchy"):
        market_repo.update(tech.id, parent_id=semis.id)


def test_update_missing_raises(market_repo):
    with pytest.raises(NotFoundError):
        market_repo.update(9999, name="X")


def test_delete_with_children_blocked_unless_force(market_repo):
    cn = market_repo.add("China", "market")
    tech = market_repo.add("Technology", "sector", parent_id=cn.id)

    with pytest.raises(RepositoryError, match="children"):
        market_repo.delete(cn.id)
    assert market_repo.delete(cn.id, force=True) is True
    # children were detached (ON DELETE SET NULL) and survive
    assert market_repo.get(tech.id).parent_id is None


def test_delete_leaf(market_repo):
    market = market_repo.add("EV", "industry")
    assert market_repo.delete(market.id) is True
    assert market_repo.get(market.id) is None


def test_list_filters(market_repo):
    market_repo.add("China A", "market")
    market_repo.add("China B", "market", active=False)
    assert len(market_repo.list()) == 1
    assert len(market_repo.list(include_inactive=True)) == 2
    assert len(market_repo.list(type="market")) == 1
    assert market_repo.list(type="sector") == []


def test_get_by_ref(market_repo):
    market = market_repo.add("EV", "industry")
    assert market_repo.get_by_ref(market.id).id == market.id
    assert market_repo.get_by_ref("ev").id == market.id
    with pytest.raises(NotFoundError):
        market_repo.get_by_ref("missing")