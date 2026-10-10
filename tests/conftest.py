"""Shared pytest fixtures: in-memory DB, isolated project roots, repositories.

All tests run offline without an API key (specs §15). The ``invest_watch``
package path is made importable even when the editable install is stale.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.config_manager import load_config
from invest_watch.db_manager import DatabaseManager
from invest_watch.market_repository import Market, MarketRepository
from invest_watch.source_repository import Source, SourceRepository
from invest_watch.exceptions import ConfigError

MINIMAL_CONFIG = """\
[app]
timezone = "Europe/Paris"

[storage]
db_path = "data/invest.db"

[llm]
enabled = false
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate every test from the machine's real invest-watch environment."""
    for var in (
        "WATSONX_API_KEY",
        "WATSONX_URL",
        "WATSONX_MODEL_ID",
        "WATSONX_PROJECT_ID",
        "INVEST_WATCH_HOME",
        "BRAVE_API_KEY",
        "FINNHUB_API_KEY",
        "AZURE_TRANSLATOR_KEY",
        "AZURE_TRANSLATOR_REGION",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def project(tmp_path) -> Path:
    """A temporary project root with ``conf/config.toml`` (minimal)."""
    root = tmp_path / "proj"
    (root / "conf").mkdir(parents=True)
    (root / "conf" / "config.toml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    return root


@pytest.fixture()
def config(project) -> object:
    return load_config(project / "conf" / "config.toml")


@pytest.fixture()
def db():
    manager = DatabaseManager(":memory:")
    manager.apply_migrations()
    yield manager
    manager.close()


@pytest.fixture()
def db_v1():
    """Database migrated only to v001 (for idempotent v1 -> v2 migration tests)."""
    manager = DatabaseManager(":memory:")
    manager.apply_migrations(up_to=1)
    yield manager
    manager.close()


@pytest.fixture()
def company_repo(db) -> CompanyRepository:
    return CompanyRepository(db)


@pytest.fixture()
def market_repo(db) -> MarketRepository:
    return MarketRepository(db)


@pytest.fixture()
def source_repo(db) -> SourceRepository:
    return SourceRepository(db)


@pytest.fixture()
def seeded_market(market_repo) -> Market:
    return market_repo.add("China A", "market", code="CN.A")


@pytest.fixture()
def seeded_company(company_repo) -> Company:
    return company_repo.add(Company(name="BYD", ticker="BYDDY", isin="CNE100000296", country_code="CN"))


@pytest.fixture()
def seeded_source(source_repo) -> Source:
    return source_repo.add(Source(name="HKEX News RSS", type="rss", url="https://example.invalid/feed.xml"))


def make_config(root: Path, content: str) -> Path:
    """Write a custom config.toml into a project root and return its path."""
    conf_dir = root / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    path = conf_dir / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def invalid_config_path(tmp_path) -> Path:
    """A project root whose config.toml is invalid TOML."""
    return make_config(tmp_path, "this is = [not valid")


@pytest.fixture()
def missing_config_path(project) -> Path:
    """A project root with a MISSING config.toml referenced explicitly."""
    return project / "conf" / "does-not-exist.toml"