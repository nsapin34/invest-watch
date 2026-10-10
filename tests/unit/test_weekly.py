"""Weekly per-company summaries (`invest-watch weekly`) - offline, LLMStub only."""

import datetime as dt
import json
from argparse import Namespace

import pytest

from invest_watch.article_repository import ArticleRepository
from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.weekly import WeeklySummarizer, build_weekly_prompt, iso_week
from invest_watch.weekly_config import WeeklySection

from tests.stubs.llm_stub import LLMStub

WEEK = "2026-W40"  # Mon 2026-09-28 .. Sun 2026-10-04


@pytest.fixture
def seeded(db):
    repo = CompanyRepository(db)
    catl = repo.add(Company(name="CATL"))
    hesai = repo.add(Company(name="Hesai"))
    arts = ArticleRepository(db)
    run = arts.create_run("2026-10-01", "2026-09-30T00:00:00Z", "2026-09-30T23:59:59Z")

    def add(url, title, day, importance, company, content="texte", content_fr=None, lang="en"):
        a = arts.insert_candidate(run_id=run.id, title=title, url=url, published_at=f"2026-{day}T08:00:00+02:00",
                                  content=content, language=lang, content_fr=content_fr)
        db.execute("UPDATE article SET importance_level = ?, relevance_score = 0.8 WHERE id = ?", (importance, a.id))
        db.execute("INSERT INTO article_company (article_id, company_id, mention_count, relevance_in_article)"
                   " VALUES (?, ?, 1, 0.8)", (a.id, company.id))
        return a

    add("https://ex.com/1", "CATL low", "09-29", "faible", catl)
    add("https://ex.com/2", "CATL critical", "09-30", "critique", catl,
        content="\u5b81\u5fb7\u65f6\u4ee3", content_fr="CATL texte en francais", lang="zh")
    add("https://ex.com/3", "CATL outside week", "10-06", "critique", catl)
    add("https://ex.com/4", "CATL unrelated", "10-01", "haute", catl)
    db.execute("UPDATE article SET status = 'unrelated' WHERE title = 'CATL unrelated'")
    return {"catl": catl, "hesai": hesai}


def _summ(db, tmp_path, llm=None, **cfg):
    return WeeklySummarizer(db, WeeklySection(**cfg), llm_client=llm, model_id="m", reports_dir=tmp_path)


def test_iso_week_parsing_and_default():
    w = iso_week("2026-w41")
    assert (w.label, w.start, w.end) == ("2026-W41", dt.date(2026, 10, 5), dt.date(2026, 10, 11))
    last = iso_week(today=dt.date(2026, 10, 10))  # Saturday of W41 -> previous full week
    assert last.label == "2026-W40" and last.start == dt.date(2026, 9, 28)
    assert iso_week(today=dt.date(2026, 10, 5)).label == "2026-W40"  # Monday
    with pytest.raises(ValueError):
        iso_week("2026-41")


def test_article_selection_window_status_and_order(db, seeded, tmp_path):
    s = _summ(db, tmp_path)
    arts = s.articles_for(seeded["catl"].id, iso_week(WEEK))
    assert [a["title"] for a in arts] == ["CATL critical", "CATL low"]


def test_one_llm_call_per_company_with_french_text(db, seeded, tmp_path):
    stub = LLMStub(responses=["### Faits marquants de la semaine\n- ok [x]"])
    results = _summ(db, tmp_path, stub).run(iso_week(WEEK))
    catl = next(r for r in results if r.company_name == "CATL")
    hesai = next(r for r in results if r.company_name == "Hesai")
    assert stub.call_count == 1  # Hesai has no article -> no call
    assert catl.llm_used and "Faits marquants" in catl.content
    prompt = stub.call_log[0]["prompt"]
    assert "CATL texte en francais" in prompt  # content_fr preferred
    assert "fran\u00e7ais" in prompt and "2026-W40" in prompt
    assert not hesai.llm_used and "Aucun article" in hesai.content
    row = db.query_one("SELECT * FROM weekly_summary WHERE company_id = ?", (seeded["catl"].id,))
    assert row["llm_used"] == 1 and row["article_count"] == 2 and row["model_id"] == "m"
    md = (tmp_path / "weekly" / "2026-W40.md").read_text(encoding="utf-8")
    assert "## CATL" in md and "[FR](https://translate.google.com/" in md
    data = json.loads((tmp_path / "weekly" / "2026-W40.json").read_text(encoding="utf-8"))
    assert data["week"] == "2026-W40" and len(data["companies"]) == 2


def test_already_done_is_skipped_unless_force(db, seeded, tmp_path):
    stub = LLMStub(responses=["v1", "v2"])
    _summ(db, tmp_path, stub).run(iso_week(WEEK), company="CATL")
    again = _summ(db, tmp_path, stub).run(iso_week(WEEK), company="CATL")
    assert stub.call_count == 1 and again[0].skipped and again[0].content == "v1"
    forced = _summ(db, tmp_path, stub).run(iso_week(WEEK), company="catl", force=True)
    assert stub.call_count == 2 and forced[0].content == "v2"


def test_llm_failure_degrades_and_is_retried_next_time(db, seeded, tmp_path):
    failing = LLMStub(raise_after=0)
    r = _summ(db, tmp_path, failing).run(iso_week(WEEK), company="CATL")[0]
    assert not r.llm_used and "d\u00e9grad\u00e9" in r.content and r.error
    assert "CATL critical" in r.content
    ok = LLMStub(responses=["recovered"])
    r2 = _summ(db, tmp_path, ok).run(iso_week(WEEK), company="CATL")[0]
    assert ok.call_count == 1 and r2.llm_used and r2.content == "recovered"


def test_use_llm_false_never_calls(db, seeded, tmp_path):
    stub = LLMStub(responses=["x"])
    r = _summ(db, tmp_path, stub, use_llm=False).run(iso_week(WEEK), company="CATL")[0]
    assert stub.call_count == 0 and not r.llm_used


def test_dry_run_no_call_no_write(db, seeded, tmp_path):
    stub = LLMStub(responses=["x"])
    r = _summ(db, tmp_path, stub).run(iso_week(WEEK), company="CATL", dry_run=True)[0]
    assert stub.call_count == 0 and len(r.articles) == 2 and r.prompt
    assert db.query_one("SELECT COUNT(*) AS n FROM weekly_summary")["n"] == 0
    assert not (tmp_path / "weekly").exists()


def test_input_caps(db, seeded, tmp_path):
    r = _summ(db, tmp_path, max_articles_per_company=1).run(iso_week(WEEK), company="CATL", dry_run=True)[0]
    assert len(r.articles) == 1
    r = _summ(db, tmp_path, max_input_chars=10).run(iso_week(WEEK), company="CATL", dry_run=True)[0]
    assert r.articles == [] and r.prompt == ""


def test_unknown_company_raises(db, seeded, tmp_path):
    with pytest.raises(ValueError):
        _summ(db, tmp_path).run(iso_week(WEEK), company="Nope")


def test_prompt_contains_sections():
    p = build_weekly_prompt("CATL", iso_week(WEEK), ["[abc] 2026-09-30 (haute) t\ntext"])
    for section in ("Faits marquants", "Chiffres cl", "Catalyseurs", "Points \u00e0 v\u00e9rifier"):
        assert section in p


def test_weekly_llm_ignores_daily_llm_flag(config):
    from invest_watch.weekly import build_weekly_llm_client
    object.__setattr__(config.llm, "enabled", False)
    client = build_weekly_llm_client(config)
    assert client is not None and client._cfg.enabled is True
    object.__setattr__(config, "weekly", WeeklySection(use_llm=False))
    assert build_weekly_llm_client(config) is None


def test_cli_weekly_with_stub(db, seeded, config, tmp_path, capsys):
    from invest_watch.cli import EXIT_OK, _cmd_weekly
    object.__setattr__(config.storage, "reports_dir", tmp_path)
    args = Namespace(week=WEEK, company=None, force=False, dry_run=False)
    assert _cmd_weekly(args, config, db, llm_client=LLMStub(responses=["resume"])) == EXIT_OK
    out = capsys.readouterr().out
    assert "weekly 2026-W40" in out and "LLM calls: 1" in out
    args = Namespace(week="bad", company=None, force=False, dry_run=True)
    assert _cmd_weekly(args, config, db) != EXIT_OK


def test_config_weekly_section(project):
    from invest_watch.config_manager import load_config
    path = project / "conf" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + "\n[weekly]\nuse_llm = false\nmax_articles_per_company = 5\n",
                    encoding="utf-8")
    cfg = load_config(path)
    assert cfg.weekly.use_llm is False and cfg.weekly.max_articles_per_company == 5


def test_prompt_forbids_share_price_figures():
    p = build_weekly_prompt("CATL", iso_week(WEEK), ["[abc] x"])
    assert "ne donne JAMAIS de cours" in p and "tendance qualitative" in p
