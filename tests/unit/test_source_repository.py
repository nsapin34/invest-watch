"""Unit tests for SourceRepository (CRUD, health counters, ping)."""

import httpx
import pytest
import respx

from invest_watch.exceptions import DuplicateEntityError, NotFoundError, RepositoryError
from invest_watch.source_repository import Source


def test_add_and_get_roundtrip(source_repo):
    source = source_repo.add(Source(name="HKEX", type="rss", url="https://example.com/feed.xml"))
    loaded = source_repo.get(source.id)
    assert loaded.name == "HKEX"
    assert loaded.type == "rss"
    assert loaded.priority == 5
    assert loaded.lang == "*"
    assert loaded.error_count == 0


def test_duplicate_name_rejected(source_repo):
    source_repo.add(Source(name="HKEX", type="rss", url="https://a.example.com"))
    with pytest.raises(DuplicateEntityError):
        source_repo.add(Source(name="HKEX", type="rss", url="https://b.example.com"))


def test_invalid_type_rejected_when_building():
    with pytest.raises(ValueError):
        Source(name="X", type="carrier-pigeon", url="http://x")


def test_get_by_name_and_ref(source_repo, seeded_source):
    assert source_repo.get_by_name("HKEX News RSS").id == seeded_source.id
    assert source_repo.get_by_ref(seeded_source.id).id == seeded_source.id
    assert source_repo.get_by_ref("HKEX News RSS").id == seeded_source.id
    with pytest.raises(NotFoundError):
        source_repo.get_by_ref("nope")


def test_update_fields(source_repo, seeded_source):
    updated = source_repo.update(seeded_source.id, url="https://new.example.com", priority=1)
    assert updated.url == "https://new.example.com"
    assert updated.priority == 1
    with pytest.raises(RepositoryError, match="unknown fields"):
        source_repo.update(seeded_source.id, nope=1)  # type: ignore[call-arg]


def test_update_invalid_type_rejected(source_repo, seeded_source):
    with pytest.raises(RepositoryError, match="type"):
        source_repo.update(seeded_source.id, type="carrier-pigeon")


def test_delete(source_repo, seeded_source):
    assert source_repo.delete(seeded_source.id) is True
    assert source_repo.get(seeded_source.id) is None
    assert source_repo.delete(seeded_source.id) is False


def test_record_success(source_repo, seeded_source):
    source_repo.record_success(seeded_source.id, at="2026-09-01T00:00:00Z")
    source = source_repo.get(seeded_source.id)
    assert source.last_success_at == "2026-09-01T00:00:00Z"
    assert source.error_count == 0


def test_record_error_increments_counter(source_repo, seeded_source):
    source_repo.record_error(seeded_source.id, at="2026-09-01T00:00:00Z")
    source_repo.record_error(seeded_source.id, at="2026-09-02T00:00:00Z")
    source = source_repo.get(seeded_source.id)
    assert source.last_error_at == "2026-09-02T00:00:00Z"
    assert source.error_count == 2


def test_list_orders_by_priority_and_filters_active(source_repo):
    source_repo.add(Source(name="B", type="rss", url="https://b", priority=5))
    source_repo.add(Source(name="A", type="rss", url="https://a", priority=1))
    source_repo.add(Source(name="C", type="rss", url="https://c", active=False))
    names = [s.name for s in source_repo.list()]
    assert names == ["A", "B"]  # C hidden (inactive), ordered by priority
    assert len(source_repo.list(include_inactive=True)) == 3


def test_upsert_by_name_inserts_then_updates(source_repo):
    inserted = source_repo.upsert_by_name(Source(name="Feed", type="rss", url="https://one"))
    assert inserted.id is not None

    updated = source_repo.upsert_by_name(
        Source(name="Feed", type="rss", url="https://two", priority=2, active=False)
    )
    assert updated.id == inserted.id  # same row
    assert updated.url == "https://two"
    assert updated.priority == 2
    assert updated.active is False


@respx.mock
def test_ping_success_updates_last_success(source_repo, seeded_source):
    respx.get(seeded_source.url).mock(return_value=httpx.Response(200, text="<rss/>"))
    assert source_repo.ping(seeded_source.id) is True
    source = source_repo.get(seeded_source.id)
    assert source.last_success_at is not None
    assert source.error_count == 0


@respx.mock
def test_ping_failure_updates_last_error(source_repo, seeded_source):
    respx.get(seeded_source.url).mock(return_value=httpx.Response(503, text="busy"))
    assert source_repo.ping(seeded_source.id) is False
    source = source_repo.get(seeded_source.id)
    assert source.last_error_at is not None
    assert source.error_count == 1


@respx.mock
def test_ping_network_error_never_raises(source_repo, seeded_source):
    respx.get(seeded_source.url).mock(side_effect=httpx.ConnectError("offline"))
    assert source_repo.ping(seeded_source.id) is False
    assert source_repo.get(seeded_source.id).error_count == 1


def test_ping_unknown_source_raises(source_repo):
    with pytest.raises(NotFoundError):
        source_repo.ping(9999)