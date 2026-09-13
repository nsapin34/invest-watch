"""Unit tests for Classifier module (Step 3.2)."""

import pytest

from invest_watch.article_repository import ArticleRepository
from invest_watch.classifier import Classifier
from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.config_manager import ScoringSection
from invest_watch.market_repository import Market, MarketRepository
from invest_watch.source_repository import Source, SourceRepository


@pytest.fixture
def setup_data(db):
    """Seed companies, aliases, markets and sources for classifier tests."""
    c_repo = CompanyRepository(db)
    m_repo = MarketRepository(db)
    s_repo = SourceRepository(db)
    a_repo = ArticleRepository(db)

    # Markets
    m_china = m_repo.add("China", "market")
    m_ev = m_repo.add("Electric Vehicles", "industry", parent_id=m_china.id)

    # Companies
    c_byd = c_repo.add(
        Company(
            name="BYD",
            ticker="BYDDY",
            isin="CNE100000296",
        ),
        aliases=["比亚迪", "BYD Company"],
        markets=[(m_china.id, 1.0), (m_ev.id, 1.0)],
    )
    c_nio = c_repo.add(
        Company(
            name="NIO",
            ticker="NIO",
        ),
        aliases=["蔚来"],
        markets=[(m_ev.id, 1.0)],
    )

    # Sources
    s_hkex = s_repo.add(Source(name="HKEX News", type="rss", url="https://hkex.test", priority=2))
    s_web = s_repo.add(Source(name="General Web", type="search", url="https://web.test", priority=3))

    # Daily run
    run = a_repo.create_run("2026-09-13", "2026-09-12T00:00:00Z", "2026-09-12T23:59:59Z")

    return {
        "c_byd": c_byd,
        "c_nio": c_nio,
        "m_china": m_china,
        "m_ev": m_ev,
        "s_hkex": s_hkex,
        "s_web": s_web,
        "run": run,
        "a_repo": a_repo,
    }


def test_classify_text_byd_earnings(db, setup_data):
    classifier = Classifier(db)
    title = "BYD Reports Record Q3 Financial Results with Strong Profit Growth"
    content = "BYD Company announced its quarterly earnings. The revenue exceeded forecasts driven by battery innovation."

    comps, mkts, cats, rel_score, importance = classifier.classify_text(
        title=title,
        content=content,
        source_priority=2,
    )

    # Check Company match
    assert len(comps) >= 1
    assert comps[0].company.name == "BYD"
    assert comps[0].title_mentions >= 1

    # Check Market match (inherited from company)
    mkt_names = [m.market.name for m in mkts]
    assert "Electric Vehicles" in mkt_names
    assert "China" in mkt_names

    # Check Category match
    cat_names = [c.name for c in cats]
    assert "financial_results" in cat_names

    # Check scores
    assert rel_score >= 0.7
    # Financial results + priority 2 source + high relevance -> critique or haute
    assert importance in ("critique", "haute")


def test_classify_cjk_alias_matching(db, setup_data):
    classifier = Classifier(db)
    title = "比亚迪 发布新一代刀片电池"
    content = "中国新能源汽车领军企业 比亚迪 宣布技术突破。"

    comps, mkts, cats, rel_score, importance = classifier.classify_text(
        title=title,
        content=content,
        source_priority=2,
    )

    assert any(c.company.name == "BYD" for c in comps)
    assert any(c.name == "product" for c in cats)


def test_classify_article_persists_junctions(db, setup_data):
    classifier = Classifier(db)
    a_repo = setup_data["a_repo"]
    run = setup_data["run"]
    s_hkex = setup_data["s_hkex"]
    c_byd = setup_data["c_byd"]

    article = a_repo.insert_candidate(
        run_id=run.id,
        title="SEC filing disclosure regarding BYD expansion",
        url="https://hkex.test/filing-byd-01",
        source_id=s_hkex.id,
        content="BYD filed official regulatory documents today.",
    )
    assert article is not None

    result = classifier.classify_article(article.id)

    assert result.article_id == article.id
    assert any(c.company.id == c_byd.id for c in result.companies)
    assert any(cat.name == "regulator" for cat in result.categories)

    # Verify database junctions
    comp_rows = db.query("SELECT * FROM article_company WHERE article_id = ?", (article.id,))
    assert len(comp_rows) >= 1
    assert comp_rows[0]["company_id"] == c_byd.id

    cat_rows = db.query(
        "SELECT c.name FROM article_category ac JOIN category c ON ac.category_id = c.id WHERE ac.article_id = ?",
        (article.id,),
    )
    cat_names = [r["name"] for r in cat_rows]
    assert "regulator" in cat_names

    # Verify article score update in DB
    art_row = db.query_one("SELECT relevance_score, importance_level, status FROM article WHERE id = ?", (article.id,))
    assert art_row["relevance_score"] >= 0.5
    assert art_row["importance_level"] in ("haute", "critique")


def test_classify_unrelated_article(db, setup_data):
    classifier = Classifier(db)
    a_repo = setup_data["a_repo"]
    run = setup_data["run"]
    s_web = setup_data["s_web"]

    article = a_repo.insert_candidate(
        run_id=run.id,
        title="Local bakery opens in small town",
        url="https://web.test/bakery-01",
        source_id=s_web.id,
        content="Fresh bread and croissants are served every morning.",
    )
    assert article is not None

    result = classifier.classify_article(article.id)
    assert len(result.companies) == 0
    assert result.relevance_score <= 0.2

    art_row = db.query_one("SELECT status, relevance_score FROM article WHERE id = ?", (article.id,))
    assert art_row["status"] == "unrelated"
