"""Unit tests for CompanyRepository (CRUD, aliases, market links, cap)."""

import pytest

from invest_watch.company_repository import MAX_COMPANIES, Alias, Company, CompanyRepository
from invest_watch.exceptions import DuplicateEntityError, NotFoundError, RepositoryError


def _make(name="BYD", **overrides):
    return Company(name=name, **overrides)


def test_add_and_get_roundtrip(company_repo):
    company = company_repo.add(_make(ticker="BYDDY", isin="CNE100000296", country_code="CN"))
    assert company.id is not None
    assert company.created_at and company.updated_at

    loaded = company_repo.get(company.id)
    assert loaded.name == "BYD"
    assert loaded.ticker == "BYDDY"
    assert loaded.isin == "CNE100000296"
    assert loaded.active is True


def test_get_by_name_is_case_insensitive(company_repo):
    company = company_repo.add(_make())
    assert company_repo.get_by_name("byd").id == company.id
    assert company_repo.get_by_name("BYD").id == company.id


def test_duplicate_name_rejected(company_repo):
    company_repo.add(_make())
    with pytest.raises(DuplicateEntityError):
        company_repo.add(_make())


def test_duplicate_name_case_insensitive_rejected(company_repo):
    company_repo.add(_make())
    with pytest.raises(DuplicateEntityError):
        company_repo.add(_make(name="byd"))


def test_duplicate_isin_rejected(company_repo):
    company_repo.add(_make(isin="CNE100000296"))
    with pytest.raises(DuplicateEntityError):
        company_repo.add(_make(name="Other", isin="CNE100000296"))


def test_get_by_ticker_and_isin(company_repo):
    company = company_repo.add(_make(ticker="BYDDY", isin="CNE100000296"))
    assert company_repo.get_by_ticker("byddy").id == company.id
    assert company_repo.get_by_isin("CNE100000296").id == company.id


def test_update_fields_and_bump_timestamp(company_repo):
    company = company_repo.add(_make())
    updated = company_repo.update(company.id, ticker="XYZ", country_code="US")
    assert updated.ticker == "XYZ"
    assert updated.country_code == "US"
    assert updated.updated_at >= company.updated_at


def test_update_to_duplicate_isin_fails(company_repo):
    first = company_repo.add(_make())
    second = company_repo.add(_make(name="Other", isin="ISSIN002"))
    with pytest.raises(DuplicateEntityError):
        company_repo.update(first.id, isin="ISSIN002")


def test_update_unknown_field_rejected(company_repo):
    company = company_repo.add(_make())
    with pytest.raises(RepositoryError, match="unknown fields"):
        company_repo.update(company.id, nope=1)  # type: ignore[call-arg]


def test_update_missing_company_raises(company_repo):
    with pytest.raises(NotFoundError):
        company_repo.update(9999, ticker="X")


def test_delete_company(company_repo, market_repo):
    company = company_repo.add(_make())
    market = market_repo.add("EV", "industry")
    company_repo.attach_market(company.id, market.id)
    company_repo.set_aliases(company.id, ["比亚迪"])

    assert company_repo.delete(company.id) is True
    assert company_repo.get(company.id) is None
    # junctions and aliases cascaded
    assert company_repo.markets_for(company.id) == []
    assert company_repo.aliases_for(company.id) == []
    assert company_repo.delete(company.id) is False


def test_search_vectors(company_repo):
    company_repo.add(_make(ticker="BYDDY", website="https://www.byd.com"))
    company_repo.add(_make(name="Tesla", ticker="TSLA", website="https://www.tesla.com"))
    assert len(company_repo.search("byd")) == 1
    assert len(company_repo.search("tesla.com")) == 1
    assert len(company_repo.search("BYD")) == 1
    assert company_repo.search("nothing-here") == []


def test_list_only_active_by_default(company_repo):
    company_repo.add(_make())
    company_repo.add(_make(name="Inactive Co", active=False))
    assert [c.name for c in company_repo.list()] == ["BYD"]
    assert len(company_repo.list(include_inactive=True)) == 2


def test_set_aliases_replaces(company_repo):
    company = company_repo.add(_make())
    changed = company_repo.set_aliases(company.id, ["比亚迪", "BYD Company"])
    assert [alias.term for alias in changed] == ["比亚迪", "BYD Company"]
    company_repo.set_aliases(company.id, ["BYD Ltd"])
    terms = [alias.term for alias in company_repo.aliases_for(company.id)]
    assert terms == ["BYD Ltd"]


def test_set_aliases_dedupes_and_ignores_empty(company_repo):
    company = company_repo.add(_make())
    changed = company_repo.set_aliases(company.id, ["  a  ", "", "a", "b", "B"])
    assert [alias.term for alias in changed] == ["a", "b"]


def test_add_alias_idempotent(company_repo):
    company = company_repo.add(_make())
    first = company_repo.add_alias(company.id, " 比亚迪 ")
    assert first.term == "比亚迪"
    # Adding the same term again returns the existing row (no duplicate)
    again = company_repo.add_alias(company.id, "比亚迪")
    assert again.id == first.id


def test_remove_alias(company_repo):
    company = company_repo.add(_make())
    company_repo.set_aliases(company.id, ["a", "b"])
    assert company_repo.remove_alias(company.id, "a") is True
    assert company_repo.remove_alias(company.id, "a") is False
    assert [alias.term for alias in company_repo.aliases_for(company.id)] == ["b"]


def test_attach_detach_market_with_weight(company_repo, market_repo):
    company = company_repo.add(_make())
    market = market_repo.add("EV", "industry")
    company_repo.attach_market(company.id, market.id, weight=1.5)
    linked = company_repo.markets_for(company.id)
    assert len(linked) == 1
    assert linked[0][0].name == "EV"
    assert linked[0][1] == 1.5

    # upsert: re-attaching updates the weight, no duplicate row
    company_repo.attach_market(company.id, market.id, weight=2.0)
    assert len(company_repo.markets_for(company.id)) == 1
    assert company_repo.markets_for(company.id)[0][1] == 2.0

    assert company_repo.detach_market(company.id, market.id) is True
    assert company_repo.markets_for(company.id) == []


def test_get_by_ref_id_and_name(company_repo):
    company = company_repo.add(_make())
    assert company_repo.get_by_ref(company.id).id == company.id
    assert company_repo.get_by_ref("byd").id == company.id
    with pytest.raises(NotFoundError):
        company_repo.get_by_ref("missing")


def test_company_cap_enforced(company_repo):
    for i in range(MAX_COMPANIES):
        company_repo.add(_make(name=f"Company {i}"))
    with pytest.raises(RepositoryError, match="limit"):
        company_repo.add(_make(name="One too many"))


def test_alias_entity_type_is_company(company_repo):
    company = company_repo.add(_make())
    alias = company_repo.add_alias(company.id, "term")
    assert isinstance(alias, Alias)