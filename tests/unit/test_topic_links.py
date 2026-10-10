"""v010 thematic links: topic score, direct/sector/peer rules, weekly quotas, reclassify."""

from argparse import Namespace

import pytest

from invest_watch.article_repository import ArticleRepository
from invest_watch.classifier import Classifier, _count_term, topic_score
from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.weekly import (
    GROUP_LABELS, WeeklySummarizer, _article_block, build_weekly_prompt, group_counts, iso_week,
    select_with_quotas,
)
from invest_watch.weekly_config import WeeklySection

KW = ("battery", "batteries", "LFP", "lithium", "sodium-ion", "\u7535\u6c60", "EV")
PEERS = ("BYD", "LG Energy Solution", "Ford Motor")


@pytest.fixture
def catl(db):
    return CompanyRepository(db).add(Company(name="CATL", topic_keywords=KW, peers=PEERS))


def _links(db, title, content=""):
    comps, *_ = Classifier(db).classify_text(title, content)
    return {m.company.name: m for m in comps}


# -- topic score / term matching ----------------------------------------------

def test_topic_score_distinct_hits_capped():
    assert topic_score(KW, "battery battery battery") == 0.15
    assert topic_score(KW, "Battery and LFP with lithium") == 0.45
    assert topic_score(KW * 3, " ".join(KW)) == 1.0
    assert topic_score((), "battery") == 0.0 and topic_score(KW, "") == 0.0


def test_cjk_keywords_match_as_substring():
    assert topic_score(KW, "\u5b81\u5fb7\u65f6\u4ee3\u52a8\u529b\u7535\u6c60\u4e1a\u52a1") == 0.15


def test_short_terms_are_case_sensitive():
    assert _count_term("SiC", "the quote [sic] is wrong") == 0
    assert _count_term("SiC", "SiC wafers for EVs") == 1
    assert _count_term("BP", "bp of 120 mmHg") == 0 and _count_term("BP", "BP raises output") == 1
    assert _count_term("EV", "every ev charger") == 0 and _count_term("EV", "EV sales") == 1
    assert _count_term("LNG", "LNG cargo") == 1 and _count_term("LNG", "lng") == 0
    assert _count_term("Shell plc", "SHELL PLC results") == 1  # long: case-insensitive
    assert _count_term("Ford Motor", "Gerald Ford") == 0 and _count_term("Ford Motor", "Ford Motor Co") == 1
    assert _count_term("", "x") == 0


def test_lowercase_ev_word_does_not_score():
    assert topic_score(("EV",), "they ev entually") == 0.0


# -- link rules -------------------------------------------------------------------

def test_direct_link_relevance_formula(db, catl):
    m = _links(db, "CATL opens plant", "CATL battery LFP")["CATL"]
    assert m.link_type == "direct"
    assert m.relevance == pytest.approx(0.4 + 0.2 + 0.4 * 0.3)
    body = _links(db, "Plant news", "CATL builds it")["CATL"]
    assert body.link_type == "direct" and body.relevance == pytest.approx(0.4)


def test_mention_count_no_longer_dominates(db, catl):
    many = _links(db, "News", "CATL " * 20)["CATL"]
    one = _links(db, "News", "CATL battery lithium LFP sodium-ion")["CATL"]
    assert one.relevance > many.relevance


def test_sector_link_above_threshold(db, catl):
    m = _links(db, "Battery market", "lithium prices and LFP cells")["CATL"]
    assert m.link_type == "sector" and m.relevance == pytest.approx(0.4 * 0.45)
    assert "CATL" not in _links(db, "Battery market", "lithium prices")  # 0.3 < 0.4


def test_sector_threshold_configurable(db, catl):
    from invest_watch.config_manager import ScoringSection
    sc = ScoringSection(0.5, "moyenne", ("regulator",), (1, 2), topic_link_min=0.3)
    comps, *_ = Classifier(db, sc).classify_text("Battery market", "lithium prices")
    assert comps and comps[0].link_type == "sector"


def test_peer_link_requires_topic(db, catl):
    m = _links(db, "BYD unveils new battery", "with lithium cells")["CATL"]
    assert m.link_type == "peer" and m.relevance == pytest.approx(0.1 + 0.4 * 0.3)
    assert "CATL" not in _links(db, "BYD unveils new battery", "")  # 1 keyword < peer_min_topic_hits
    assert "CATL" not in _links(db, "BYD CEO interview", "about football")


def test_noisy_peer_forms(db, catl):
    assert "CATL" not in _links(db, "Gerald Ford library battery exhibit", "")  # 'Ford Motor' only
    assert _links(db, "Ford Motor battery deal", "LFP")["CATL"].link_type == "peer"


def test_direct_beats_peer_and_sorted_first(db, catl):
    CompanyRepository(db).add(Company(name="Hesai", topic_keywords=("lidar",)))
    comps, *_ = Classifier(db).classify_text("Battery LFP lithium news with Hesai", "")
    assert [m.company.name for m in comps] == ["Hesai"]  # sector link yields to a direct link


def test_classify_article_persists_link_and_revives_unrelated(db, catl):
    arts = ArticleRepository(db)
    run = arts.create_run("2026-10-01", "2026-09-30T00:00:00Z", "2026-09-30T23:59:59Z")
    a = arts.insert_candidate(run_id=run.id, title="Battery market outlook", url="https://ex.com/b",
                              published_at="2026-09-30T08:00:00Z", content="lithium LFP sodium-ion", language="en")
    db.execute("UPDATE article SET status = 'unrelated' WHERE id = ?", (a.id,))
    res = Classifier(db).classify_article(a.id)
    assert res.relevance_score == pytest.approx(0.24)
    row = db.query_one("SELECT link_type FROM article_company WHERE article_id = ?", (a.id,))
    assert row["link_type"] == "sector"
    assert db.query_one("SELECT status FROM article WHERE id = ?", (a.id,))["status"] == "new"


def test_link_type_check_constraint(db, catl):
    import sqlite3
    arts = ArticleRepository(db)
    run = arts.create_run("2026-10-01", "2026-09-30T00:00:00Z", "2026-09-30T23:59:59Z")
    a = arts.insert_candidate(run_id=run.id, title="t", url="https://ex.com/c", published_at="2026-09-30T08:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO article_company (article_id, company_id, link_type) VALUES (?, ?, 'bogus')",
                   (a.id, catl.id))


# -- repository / config ---------------------------------------------------------

def test_repository_roundtrip_json(db, catl):
    repo = CompanyRepository(db)
    got = repo.get(catl.id)
    assert got.topic_keywords == KW and got.peers == PEERS
    upd = repo.update(catl.id, peers=["Panasonic"], topic_keywords=())
    assert upd.peers == ("Panasonic",) and upd.topic_keywords == ()
    db.execute("UPDATE company SET peers = 'not json' WHERE id = ?", (catl.id,))
    assert repo.get(catl.id).peers == ()


def test_config_parses_and_syncs_lists(project):
    from invest_watch.bootstrap import open_database, seed_from_config
    from invest_watch.config_manager import load_config
    path = project / "conf" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + '\n[[companies]]\nname = "CATL"\n'
                    'topic_keywords = ["battery", " Battery ", "LFP"]\npeers = ["BYD"]\n', encoding="utf-8")
    cfg = load_config(path)
    seed = [c for c in cfg.companies if c.name == "CATL"][0]
    assert seed.topic_keywords == ("battery", "LFP") and seed.peers == ("BYD",)
    db = open_database(cfg, migrate=True)
    try:
        seed_from_config(db, cfg)
        assert CompanyRepository(db).get_by_name("CATL").peers == ("BYD",)
    finally:
        db.close()


def test_config_rejects_non_list(project):
    from invest_watch.config_manager import load_config
    from invest_watch.exceptions import ConfigError
    path = project / "conf" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + '\n[[companies]]\nname = "CATL"\npeers = "BYD"\n',
                    encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


# -- weekly quotas ------------------------------------------------------------------

def _a(i, link, rel, imp="moyenne"):
    return {"id": i, "short_code": f"c{i}", "link_type": link, "relevance_in_article": rel,
            "importance_level": imp, "relevance_score": 0.5, "title": f"t{i}"}


def test_quotas_per_group_sorted_by_relevance():
    items = [_a(i, "direct", i / 10) for i in range(5)] + [_a(10 + i, "sector", 0.2) for i in range(4)]
    out = select_with_quotas(items, {"direct": 2, "sector": 1, "peer": 1}, 4)
    assert [a["link_type"] for a in out] == ["direct", "direct", "direct", "sector"]
    assert [a["id"] for a in out[:3]] == [4, 3, 2]  # peer slot unused -> best leftover


def test_importance_breaks_relevance_ties():
    out = select_with_quotas([_a(1, "direct", 0.5, "faible"), _a(2, "direct", 0.5, "critique")],
                             {"direct": 1, "sector": 0, "peer": 0}, 1)
    assert out[0]["id"] == 2


def test_quotas_trimmed_by_cap_and_unknown_group():
    items = [_a(1, "direct", 0.9), _a(2, "sector", 0.9), _a(3, "peer", 0.9), _a(4, None, 0.1)]
    out = select_with_quotas(items, {"direct": 2, "sector": 2, "peer": 2}, 2)
    assert [a["id"] for a in out] == [1, 4]
    assert group_counts(out + [_a(5, "weird", 0)]) == {"direct": 3, "sector": 0, "peer": 0}


def test_block_and_prompt_mark_groups():
    assert "(SECTEUR, moyenne)" in _article_block(_a(1, "sector", 0.3), 100)
    assert "(SOCIETE," in _article_block(_a(2, None, 0.3), 100)
    p = build_weekly_prompt("CATL", iso_week("2026-W40"), ["[x]"])
    assert "CONCURRENT" in p and "ne donne JAMAIS de cours" in p


def test_weekly_report_groups_sources(db, catl, tmp_path):
    arts = ArticleRepository(db)
    run = arts.create_run("2026-10-01", "2026-09-30T00:00:00Z", "2026-09-30T23:59:59Z")
    for i, link in enumerate(["direct", "sector", "peer", "sector"]):
        a = arts.insert_candidate(run_id=run.id, title=f"T{link}{i}", url=f"https://ex.com/{i}",
                                  published_at="2026-09-30T08:00:00Z", content="x", language="en")
        db.execute("INSERT INTO article_company (article_id, company_id, relevance_in_article, link_type)"
                   " VALUES (?, ?, 0.5, ?)", (a.id, catl.id, link))
    s = WeeklySummarizer(db, WeeklySection(use_llm=False, quota_sector=1), reports_dir=tmp_path)
    res = s.run(iso_week("2026-W40"))
    assert group_counts(res[0].articles) == {"direct": 1, "sector": 2, "peer": 1}  # leftover reused
    md = (tmp_path / "weekly" / "2026-W40.md").read_text(encoding="utf-8")
    for label in GROUP_LABELS.values():
        assert f"*{label}*" in md
    assert md.index("*Soci\u00e9t\u00e9*") < md.index("*Secteur*") < md.index("*Concurrents / partenaires*")


# -- reclassify CLI ------------------------------------------------------------------

def test_cli_reclassify(db, catl, config, capsys):
    from invest_watch.cli import EXIT_OK, _cmd_reclassify, build_parser
    arts = ArticleRepository(db)
    run = arts.create_run("2026-10-01", "2026-09-30T00:00:00Z", "2026-09-30T23:59:59Z")
    arts.insert_candidate(run_id=run.id, title="CATL results", url="https://ex.com/r1",
                          published_at="2026-09-30T08:00:00Z", content="x", language="en")
    arts.insert_candidate(run_id=run.id, title="Battery LFP lithium", url="https://ex.com/r2",
                          published_at="2026-09-30T08:00:00Z", content="x", language="en")
    arts.insert_candidate(run_id=run.id, title="Old CATL", url="https://ex.com/r3",
                          published_at="2026-08-01T08:00:00Z", content="x", language="en")
    assert _cmd_reclassify(Namespace(since="2026-09-01"), config, db) == EXIT_OK
    out = capsys.readouterr().out
    assert "reclassified 2 article(s)" in out and "direct=1" in out and "sector=1" in out
    assert _cmd_reclassify(Namespace(since="bad"), config, db) != EXIT_OK
    assert build_parser().parse_args(["reclassify", "--since", "2026-09-01"]).since == "2026-09-01"

def test_peer_min_topic_hits_configurable(db, catl):
    from invest_watch.config_manager import ScoringSection
    sc = ScoringSection(0.5, "moyenne", ("regulator",), (1, 2), peer_min_topic_hits=1)
    comps, *_ = Classifier(db, sc).classify_text("BYD unveils new battery", "")
    assert comps[0].link_type == "peer"


def test_default_thresholds():
    from invest_watch.config_manager import ScoringSection
    sc = ScoringSection(0.5, "moyenne", (), ())
    assert sc.topic_link_min == 0.45 and sc.peer_min_topic_hits == 2


def test_weekly_cap_includes_instructions(db, catl, tmp_path):
    arts = ArticleRepository(db)
    run = arts.create_run("2026-10-01", "2026-09-30T00:00:00Z", "2026-09-30T23:59:59Z")
    for i in range(5):
        a = arts.insert_candidate(run_id=run.id, title=f"CATL {i}", url=f"https://ex.com/cap{i}",
                                  published_at="2026-09-30T08:00:00Z", content="x" * 500, language="en")
        db.execute("INSERT INTO article_company (article_id, company_id, relevance_in_article) VALUES (?, ?, 0.5)",
                   (a.id, catl.id))
    overhead = len(build_weekly_prompt("CATL", iso_week("2026-W40"), []))
    cap = overhead + 1300
    s = WeeklySummarizer(db, WeeklySection(use_llm=False, max_input_chars=cap), reports_dir=tmp_path)
    r = s.run(iso_week("2026-W40"), dry_run=True)[0]
    assert len(r.prompt) <= cap and len(r.articles) == 2


# -- v010 rule: thematic links yield to direct links on other tracked companies

def test_sector_dropped_when_other_company_direct(db, catl):
    CompanyRepository(db).add(Company(name="Wasion"))
    names = _links(db, "Wasion battery LFP lithium storage", "")
    assert "Wasion" in names and "CATL" not in names
    assert _links(db, "Battery LFP lithium storage", "")["CATL"].link_type == "sector"


def test_peer_kept_when_direct_company_is_the_peer(db, catl):
    CompanyRepository(db).add(Company(name="BYD"))
    names = _links(db, "BYD battery LFP plant", "")
    assert names["BYD"].link_type == "direct" and names["CATL"].link_type == "peer"


def test_peer_dropped_when_other_direct_company_not_a_peer(db, catl):
    CompanyRepository(db).add(Company(name="Hesai"))
    names = _links(db, "BYD battery LFP deal with Hesai", "")
    assert "Hesai" in names and "CATL" not in names


# -- v011 topic_query -------------------------------------------------------------

def test_topic_query_repo_config_sync(project):
    from invest_watch.bootstrap import open_database, seed_from_config
    from invest_watch.config_manager import load_config
    path = project / "conf" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + '\n[[companies]]\nname = "CATL"\n'
                    "topic_query = 'battery LFP OR \"energy storage\"'\n", encoding="utf-8")
    cfg = load_config(path)
    assert [c.topic_query for c in cfg.companies if c.name == "CATL"] == ['battery LFP OR "energy storage"']
    db = open_database(cfg, migrate=True)
    try:
        seed_from_config(db, cfg)
        assert CompanyRepository(db).get_by_name("CATL").topic_query == 'battery LFP OR "energy storage"'
    finally:
        db.close()


def test_generate_queries_topic_after_companies_before_markets():
    from invest_watch.market_repository import Market
    from invest_watch.search_engine import generate_queries
    comp = Company(name="CATL", id=1, topic_query='battery OR "energy storage"')
    other = Company(name="Hesai", id=2)
    qs = generate_queries([(comp, [], []), (other, [], [])], [Market(name="Copper", type="market", id=5)],
                          queries_per_company=1)
    assert [q.entity_type for q in qs] == ["company", "company", "topic", "market", "market"]
    topic = qs[2]
    assert topic.text == 'battery OR "energy storage"' and not topic.is_primary
    assert topic.finnhub_symbol is None and topic.hkex_stock_id is None and topic.company_name == "CATL"
    capped = generate_queries([(comp, [], [])], [Market(name="Copper", type="market", id=5)],
                              queries_per_company=1, max_queries=2)
    assert [q.entity_type for q in capped] == ["company", "topic"]  # markets trimmed first


def test_queries_per_market_zero_emits_no_market_query():
    from invest_watch.market_repository import Market
    from invest_watch.search_engine import generate_queries
    qs = generate_queries([(Company(name="CATL", id=1), [], [])],
                          [Market(name="Copper", type="market", id=5), Market(name="EV", type="industry", id=6)],
                          queries_per_market=0)
    assert qs and not [q for q in qs if q.entity_type == "market"]


def test_reclassify_refreshes_stale_links_on_duplicates(db, catl, config, capsys):
    from invest_watch.cli import _cmd_reclassify
    al = CompanyRepository(db).add(Company(name="Air Liquide", ticker="AI"))
    arts = ArticleRepository(db)
    run = arts.create_run("2026-10-01", "2026-09-30T00:00:00Z", "2026-09-30T23:59:59Z")
    a = arts.insert_candidate(run_id=run.id, title="STARTRADER Adds 20 Stock and ETF CFDs", url="https://ex.com/cfd",
                              published_at="2026-09-30T08:00:00Z", content="AI stocks and ETF CFDs", language="en")
    db.execute("UPDATE article SET status = 'duplicate' WHERE id = ?", (a.id,))
    db.execute("INSERT INTO article_company (article_id, company_id) VALUES (?, ?)", (a.id, al.id))  # stale link
    _cmd_reclassify(Namespace(since="2026-09-01"), config, db)
    assert db.query_one("SELECT COUNT(*) AS n FROM article_company WHERE article_id = ?", (a.id,))["n"] == 0
    assert db.query_one("SELECT status FROM article WHERE id = ?", (a.id,))["status"] == "duplicate"


def test_real_air_liquide_matches_kept(db):
    al = CompanyRepository(db).add(Company(name="Air Liquide", ticker="AI"))
    for title in ("Air Liquide signs hydrogen deal", "L'Air Liquide (AI) results", "Air Liquide EPA:AI rises"):
        assert _links(db, title)["Air Liquide"].link_type == "direct"
    assert "Air Liquide" not in _links(db, "AI stocks and ETF CFDs: NVDA, AI.PA-like lists")
