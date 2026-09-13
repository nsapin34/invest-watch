"""Unit tests for config_manager (architecture Step 1 / specs §11).

Covers: TOML loading, defaults merge, ${VAR} resolution, env overrides,
path anchoring, seed parsing and the degraded example-config fallback.
"""

import pytest
from dataclasses import FrozenInstanceError

from invest_watch.config_manager import load_config
from invest_watch.exceptions import ConfigError

from tests.conftest import MINIMAL_CONFIG, make_config

VALID_CONFIG = """\
[app]
timezone = "Asia/Shanghai"
language = "en"

[storage]
db_path = "custom/invest.db"
retention_days = 45

[collect]
max_queries_per_run = 60
rate_limit_requests_per_sec = 0.2

[dedup]
title_similarity = 0.8

[scoring]
relevance_min = 0.6

[llm]
enabled = true
model = "ibm/granite-3-8b-instruct"
max_new_tokens = 500

[logging]
level = "DEBUG"
max_bytes = 1000000

[[sources]]
name = "Google News RSS"
type = "search"
url = "https://news.google.com/rss/search?q={query}"
priority = 3

[[companies]]
name = "BYD"
ticker = "BYDDY"
isin = "CNE100000296"
country = "CN"
alias = ["比亚迪", "BYD Company"]
langue = ["zh", "en"]

[[companies.markets]]
name = "Electric Vehicles"
type = "industry"
"""


def test_load_valid_config(project):
    make_config(project, VALID_CONFIG)
    cfg = load_config(project / "conf" / "config.toml")

    assert cfg.app.timezone == "Asia/Shanghai"
    assert cfg.app.language == "en"
    assert cfg.storage.db_path == (project / "custom" / "invest.db").resolve()
    assert cfg.storage.retention_days == 45
    assert cfg.collect.max_queries_per_run == 60
    assert cfg.collect.rate_limit_requests_per_sec == 0.2
    assert cfg.dedup.title_similarity == 0.8
    assert cfg.scoring.relevance_min == 0.6
    assert cfg.llm.enabled is True
    assert cfg.llm.model == "ibm/granite-3-8b-instruct"
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.max_bytes == 1_000_000


def test_defaults_merge_when_keys_missing(project):
    make_config(project, '[app]\ntimezone = "Europe/Paris"\n')
    cfg = load_config(project / "conf" / "config.toml")

    assert cfg.app.run_time == "07:00"  # default
    assert cfg.app.max_duration_minutes == 90  # default
    assert cfg.storage.backup_enabled is True  # default
    assert cfg.collect.respect_robots is True  # default
    assert cfg.collect.period_tolerance_hours == 6  # default
    assert cfg.llm.temperature == 0.1  # default
    assert cfg.dedup.new_event_lookback_days == 14  # default


def test_paths_anchored_to_project_root(project):
    make_config(project, '[storage]\ndb_path = "data/x.db"\n')
    cfg = load_config(project / "conf" / "config.toml")
    assert cfg.storage.db_path == (project / "data" / "x.db").resolve()
    assert cfg.storage.logs_dir == (project / "logs").resolve()


def test_invalid_toml_raises(invalid_config_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(invalid_config_path)


def test_missing_explicit_config_raises(missing_config_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(missing_config_path)


def test_no_config_anywhere_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("INVEST_WATCH_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="no configuration found"):
        load_config()


def test_fallback_to_example_config(project, caplog, monkeypatch):
    (project / "conf" / "config.toml").unlink()
    (project / "conf" / "config.example.toml").write_text(
        '[app]\ntimezone = "Europe/Paris"\n', encoding="utf-8"
    )
    monkeypatch.setenv("INVEST_WATCH_HOME", str(project))
    cfg = load_config()
    assert cfg.config_path.name == "config.example.toml"
    assert cfg.app.timezone == "Europe/Paris"
    assert any("fallback" in record.message for record in caplog.records)


def test_env_placeholder_resolution(project, monkeypatch):
    monkeypatch.setenv("IW_DATA_DIR", str(project / "data"))
    make_config(project, '[storage]\ndb_path = "${IW_DATA_DIR}/invest.db"\n')
    cfg = load_config(project / "conf" / "config.toml")
    assert cfg.storage.db_path == (project / "data" / "invest.db").resolve()


def test_unresolved_placeholder_becomes_empty(project):
    make_config(project, '[storage]\nreports_dir = "reports_${NO_SUCH_VAR}"\n')
    cfg = load_config(project / "conf" / "config.toml")
    assert cfg.storage.reports_dir == (project / "reports_").resolve()


def test_env_overrides_toml_for_llm(project, monkeypatch):
    make_config(project, '[llm]\nmodel = "toml-model"\nendpoint = "https://toml-endpoint"\n')
    monkeypatch.setenv("WATSONX_MODEL_ID", "env/model")
    monkeypatch.setenv("WATSONX_URL", "https://env-endpoint")
    monkeypatch.setenv("WATSONX_API_KEY", "secret-key")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "proj-123")
    cfg = load_config(project / "conf" / "config.toml")
    assert cfg.llm.model == "env/model"
    assert cfg.llm.endpoint == "https://env-endpoint"
    assert cfg.llm.api_key == "secret-key"
    assert cfg.llm.project_id == "proj-123"


def test_llm_api_key_never_comes_from_toml(project):
    make_config(project, '[llm]\napi_key = "do-not-trust-this"\n')
    cfg = load_config(project / "conf" / "config.toml")
    assert cfg.llm.api_key == ""  # never read from the TOML


def test_invalid_timezone_raises(project):
    make_config(project, '[app]\ntimezone = "Mars/Olympus"\n')
    with pytest.raises(ConfigError, match="timezone"):
        load_config(project / "conf" / "config.toml")


def test_invalid_logging_level_raises(project):
    make_config(project, '[logging]\nlevel = "LOUD"\n')
    with pytest.raises(ConfigError, match="logging.*level"):
        load_config(project / "conf" / "config.toml")


def test_type_mismatch_raises(project):
    make_config(project, '[storage]\nretention_days = "ninety"\n')
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(project / "conf" / "config.toml")


def test_seeds_parsed(project):
    make_config(project, VALID_CONFIG)
    cfg = load_config(project / "conf" / "config.toml")

    assert len(cfg.sources) == 1
    source = cfg.sources[0]
    assert source.name == "Google News RSS"
    assert source.type == "search"
    assert source.priority == 3

    assert len(cfg.companies) == 1
    company = cfg.companies[0]
    assert company.name == "BYD"
    assert company.ticker == "BYDDY"
    assert company.country_code == "CN"
    assert company.aliases == ("比亚迪", "BYD Company")
    assert company.languages == ("zh", "en")
    assert len(company.markets) == 1
    assert company.markets[0].name == "Electric Vehicles"
    assert company.markets[0].type == "industry"


def test_company_seed_requires_name(tmp_path):
    root = tmp_path / "bad"
    make_config(root, MINIMAL_CONFIG + '\n[[companies]]\nticker = "X"\n')
    with pytest.raises(ConfigError, match="companies"):
        load_config(root / "conf" / "config.toml")


def test_source_seed_requires_valid_type(tmp_path):
    root = tmp_path / "bad"
    make_config(root, MINIMAL_CONFIG + '\n[[sources]]\nname = "X"\ntype = "carrier-pigeon"\n')
    with pytest.raises(ConfigError, match="type"):
        load_config(root / "conf" / "config.toml")


def test_config_is_immutable(project, config):
    with pytest.raises(FrozenInstanceError):
        config.app.timezone = "Europe/London"  # type: ignore[misc]


def test_config_path_is_recorded(project):
    make_config(project, "")
    cfg = load_config(project / "conf" / "config.toml")
    assert cfg.config_path == (project / "conf" / "config.toml").resolve()
    assert cfg.project_root == project.resolve()