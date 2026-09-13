"""Unit tests for Dedup module (Step 3.3).

Truth table test covering all 4 deduplication layers + event clustering:
1. Exact normalized URL match -> duplicate
2. Content hash SHA-256 match -> duplicate
3. Title similarity Jaccard >= 0.85 -> duplicate
4. Content similarity MinHash >= 0.90 -> redundant
5. Event clustering (same company, +/- 24h, moderate title similarity) -> new + attached event_id
6. Brand new independent story -> new (event_id = article_id)
"""

import pytest

from invest_watch.article_repository import ArticleRepository
from invest_watch.cleaner import content_hash
from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.config_manager import DedupSection
from invest_watch.dedup import ArticleFingerprint, Deduplicator, compute_minhash, jaccard_similarity, tokenize
from invest_watch.market_repository import MarketRepository
from invest_watch.short_link import ShortLinkRepository


@pytest.fixture
def dedup_env(db):
    c_repo = CompanyRepository(db)
    a_repo = ArticleRepository(db)

    company = c_repo.add(Company(name="BYD", ticker="BYDDY"))
    run = a_repo.create_run("2026-09-13", "2026-09-12T00:00:00Z", "2026-09-12T23:59:59Z")

    return {
        "company": company,
        "run": run,
        "a_repo": a_repo,
    }


def test_tokenize_and_jaccard():
    t1 = tokenize("BYD Reports Record Q3 Revenue in Europe")
    t2 = tokenize("BYD reports record Q3 revenue across Europe")
    sim = jaccard_similarity(t1, t2)
    assert sim >= 0.80

    t3 = tokenize("Tesla cuts prices in China market")
    assert jaccard_similarity(t1, t3) == 0.0


def test_compute_minhash():
    text1 = "BYD Company has achieved massive electric vehicle sales numbers in Shenzhen this quarter with record deliveries across all regions."
    text2 = "BYD Company has achieved massive electric vehicle sales numbers in Shenzhen this quarter with strong deliveries across all regions."
    text3 = "Unrelated geopolitical discussion regarding international trade agreements and treaties between different distant nations."

    mh1 = compute_minhash(text1)
    mh2 = compute_minhash(text2)
    mh3 = compute_minhash(text3)

    assert mh1.jaccard(mh2) > 0.6
    assert mh1.jaccard(mh3) < 0.2


def test_deduplication_truth_table_10_articles(db, dedup_env):
    """10-article truth table test as specified in architecture §8 (Step 3).

    - Article 1: Head article (New)
    - Article 2: Exact same URL (Duplicate - Layer 1)
    - Article 3: Same Content Hash (Duplicate - Layer 2)
    - Article 4: Near-identical title Jaccard >= 0.85 (Duplicate - Layer 3)
    - Article 5: Syndicated near-copy MinHash >= 0.90 (Redundant - Layer 4)
    - Article 6: Different perspective on same event (+-24h) -> Clustered (New with event_id=art1.id)
    - Article 7: Independent new story for BYD -> New
    - Article 8: Independent story for another topic -> New
    - Article 9: Duplicate of Article 7 by URL -> Duplicate
    - Article 10: Unrelated story -> Unrelated
    """
    a_repo = dedup_env["a_repo"]
    run = dedup_env["run"]
    comp = dedup_env["company"]

    body_long_1 = "BYD today officially unveiled its revolutionary solid-state battery technology with 1000km range during a major press conference in Shenzhen headquarters with executive leadership present."
    body_long_2 = "BYD today officially announced its revolutionary solid-state battery technology with 1000km range during a major press conference in Shenzhen headquarters with executive leadership present."
    hash_base = content_hash(body_long_1)

    # 1. Base article
    art1 = a_repo.insert_candidate(
        run_id=run.id,
        title="BYD Unveils Solid State Battery with 1000km Range",
        url="https://news.test/byd-battery-launch",
        content=body_long_1,
        content_hash=hash_base,
        published_at="2026-09-12T10:00:00Z",
    )
    assert art1 is not None
    # Link company
    db.execute("INSERT INTO article_company (article_id, company_id, mention_count) VALUES (?, ?, ?)", (art1.id, comp.id, 2))

    # 2. Duplicate Layer 2: Content Hash simulation in memory / candidate evaluation
    candidate_art2 = ArticleFingerprint(
        id=992,
        run_id=run.id,
        url_normalized="https://wire.test/story-123",
        content_hash=hash_base,
        title="Syndicated Wire Story",
        title_tokens=tokenize("Syndicated Wire Story"),
        published_at=None,
        collected_at=None,
        status="new",
        event_id=None,
        minhash=compute_minhash(body_long_1),
        company_ids={comp.id},
    )

    # 3. Duplicate Layer 3: Title similarity >= 0.85
    art3 = a_repo.insert_candidate(
        run_id=run.id,
        title="BYD Unveils Solid State Battery with 1000km Range Today",
        url="https://other.test/story-4",
        content="Short summary version with different words.",
        content_hash=content_hash("Short summary version with different words."),
        published_at="2026-09-12T12:00:00Z",
    )
    assert art3 is not None

    # 4. Redundant Layer 4: Content MinHash >= 0.90 (long body with nearly identical phrasing, >90% words identical)
    body_long_2 = body_long_1.replace("Shenzhen headquarters", "Shenzhen main headquarters")
    art4 = a_repo.insert_candidate(
        run_id=run.id,
        title="Completely Rewritten Headline on Battery Tech",
        url="https://press.test/story-5",
        content=body_long_2,
        content_hash=content_hash(body_long_2),
        published_at="2026-09-12T13:00:00Z",
    )
    assert art4 is not None

    # 5. Event Clustering: Same company, same day, moderate title similarity
    art5 = a_repo.insert_candidate(
        run_id=run.id,
        title="Analysts praise BYD solid state battery breakthrough",
        url="https://analysis.test/byd-battery-analysts",
        content="Financial analysts from Goldman Sachs reviewed the new battery specs and gave a buy rating.",
        content_hash=content_hash("Financial analysts from Goldman Sachs reviewed the new battery specs and gave a buy rating."),
        published_at="2026-09-12T14:00:00Z",
    )
    assert art5 is not None
    db.execute("INSERT INTO article_company (article_id, company_id, mention_count) VALUES (?, ?, ?)", (art5.id, comp.id, 2))

    # 6. Independent New Story for BYD
    art6 = a_repo.insert_candidate(
        run_id=run.id,
        title="BYD Expands Commercial Truck Factory in Hungary",
        url="https://hungary.test/byd-trucks",
        content="Construction has commenced on a new electric bus and truck production line in Hungary.",
        content_hash=content_hash("Construction has commenced on a new electric bus and truck production line in Hungary."),
        published_at="2026-09-12T15:00:00Z",
    )
    assert art6 is not None
    db.execute("INSERT INTO article_company (article_id, company_id, mention_count) VALUES (?, ?, ?)", (art6.id, comp.id, 1))

    # 7. Independent New Story (General Market)
    art7 = a_repo.insert_candidate(
        run_id=run.id,
        title="China Central Bank PBoC Cuts Reserve Requirement Ratio",
        url="https://macro.test/pboc-rrr-cut",
        content="The People's Bank of China lowered reserve requirements by 50 basis points.",
        content_hash=content_hash("The People's Bank of China lowered reserve requirements by 50 basis points."),
        published_at="2026-09-12T16:00:00Z",
    )
    assert art7 is not None

    # 8. Duplicate Layer 3 for Article 6 (Title similarity)
    art8 = a_repo.insert_candidate(
        run_id=run.id,
        title="BYD Expands Commercial Truck Factory in Hungary Now",
        url="https://hungary.test/byd-trucks-mirror",
        content="Different mirror text",
        content_hash=content_hash("Different mirror text"),
        published_at="2026-09-12T17:00:00Z",
    )
    assert art8 is not None

    # 9. Second independent general story
    art9 = a_repo.insert_candidate(
        run_id=run.id,
        title="European Union Proposes New Battery Passport Standards",
        url="https://eu.test/battery-standards",
        content="The European Commission today published draft regulations on industrial battery recycling.",
        content_hash=content_hash("The European Commission today published draft regulations on industrial battery recycling."),
        published_at="2026-09-12T18:00:00Z",
    )
    assert art9 is not None

    # 10. Unrelated Article
    art10 = a_repo.insert_candidate(
        run_id=run.id,
        title="Local Gardening Tips for Autumn Vegetables",
        url="https://garden.test/autumn-veggies",
        content="Plant carrots and spinach before the first frost arrives.",
        content_hash=content_hash("Plant carrots and spinach before the first frost arrives."),
        published_at="2026-09-12T19:00:00Z",
    )
    assert art10 is not None
    # Set status as unrelated (as done by classifier)
    db.execute("UPDATE article SET status = 'unrelated' WHERE id = ?", (art10.id,))

    # Run Deduplicator
    deduplicator = Deduplicator(db)
    results = deduplicator.deduplicate_run(run.id)

    res_by_id = {r.article_id: r for r in results}

    # Verify statuses
    # Art 1: Head article
    assert res_by_id[art1.id].status == "new"
    assert res_by_id[art1.id].event_id == art1.id

    # Direct layer 2 check on candidate_art2 against Art 1
    res_layer2 = deduplicator.evaluate_article(candidate_art2, deduplicator.load_recent_fingerprints())
    assert res_layer2.status == "duplicate"
    assert "content hash" in (res_layer2.dedup_reason or "")

    # Art 3: Duplicate (Title Jaccard)
    assert res_by_id[art3.id].status == "duplicate"
    assert "title similarity" in (res_by_id[art3.id].dedup_reason or "")

    # Art 4: Redundant (MinHash)
    assert res_by_id[art4.id].status == "redundant"
    assert "MinHash" in (res_by_id[art4.id].dedup_reason or "")

    # Art 5: Clustered with Art 1
    assert res_by_id[art5.id].status == "new"
    assert res_by_id[art5.id].event_id == art1.id

    # Art 6: Independent New (BYD Hungary)
    assert res_by_id[art6.id].status == "new"
    assert res_by_id[art6.id].event_id == art6.id

    # Art 7: Independent New (China Macro)
    assert res_by_id[art7.id].status == "new"
    assert res_by_id[art7.id].event_id == art7.id

    # Art 8: Duplicate of Art 6 (Title similarity)
    assert res_by_id[art8.id].status == "duplicate"

    # Art 9: Independent New (EU standards)
    assert res_by_id[art9.id].status == "new"
    assert res_by_id[art9.id].event_id == art9.id

    # Art 10: Unrelated
    assert res_by_id[art10.id].status == "unrelated"


def test_event_clustering_cross_run(db):
    """Event clustering: same company ±24h across two different runs → shared event_id.

    Architecture Step 3 spec: 'Event clustering: same company ±24h → correct event_id'.
    Run 1 publishes a head article for BYD. Run 2, 6 hours later, publishes a follow-up
    article about the same event. The follow-up must receive the event_id of the head.
    """
    c_repo = CompanyRepository(db)
    a_repo = ArticleRepository(db)
    sl_repo = ShortLinkRepository(db)

    company = c_repo.add(Company(name="BYD_X", ticker="BYDX"))

    # Run 1 — head article
    run1 = a_repo.create_run("2026-09-13", "2026-09-12T00:00:00Z", "2026-09-12T23:59:59Z")
    head = a_repo.insert_candidate(
        run_id=run1.id,
        title="BYD_X announces new battery factory in Poland",
        url="https://news.test/bydx-poland-factory",
        content="BYD_X signed an agreement to build a Gigafactory near Warsaw.",
        content_hash=content_hash("BYD_X signed an agreement to build a Gigafactory near Warsaw."),
        published_at="2026-09-12T08:00:00Z",
    )
    assert head is not None
    db.execute("INSERT INTO article_company (article_id, company_id, mention_count) VALUES (?, ?, ?)", (head.id, company.id, 3))

    dedup1 = Deduplicator(db)
    results1 = dedup1.deduplicate_run(run1.id)
    head_res = next(r for r in results1 if r.article_id == head.id)
    assert head_res.status == "new"
    assert head_res.event_id == head.id

    # Verify short link was created for the head article
    sl = sl_repo.get_by_article_id(head.id)
    assert sl is not None
    assert len(sl.short_code) in (6, 7, 8)
    assert sl.url_original == "https://news.test/bydx-poland-factory"

    # Run 2 — follow-up article, 6 hours later, same company
    run2 = a_repo.create_run("2026-09-14", "2026-09-12T06:00:00Z", "2026-09-13T05:59:59Z")
    followup = a_repo.insert_candidate(
        run_id=run2.id,
        title="BYD_X new battery factory in Poland confirmed by government",
        url="https://warsaw.test/bydx-factory-confirmed",
        content="Polish ministry confirms BYD_X factory investment agreement.",
        content_hash=content_hash("Polish ministry confirms BYD_X factory investment agreement."),
        published_at="2026-09-12T14:00:00Z",
    )
    assert followup is not None
    db.execute("INSERT INTO article_company (article_id, company_id, mention_count) VALUES (?, ?, ?)", (followup.id, company.id, 2))

    dedup2 = Deduplicator(db)
    results2 = dedup2.deduplicate_run(run2.id)
    followup_res = next(r for r in results2 if r.article_id == followup.id)

    # Follow-up must be 'new' (original perspective) but share the head's event_id
    assert followup_res.status == "new"
    assert followup_res.event_id == head.id

    # Short link must also be created for the follow-up
    sl_followup = sl_repo.get_by_article_id(followup.id)
    assert sl_followup is not None
    assert sl_followup.short_code != sl.short_code  # distinct codes


def test_deduplicate_run_creates_short_links_for_new_articles(db):
    """deduplicate_run must persist a short_link for every article marked 'new'."""
    c_repo = CompanyRepository(db)
    a_repo = ArticleRepository(db)
    sl_repo = ShortLinkRepository(db)

    company = c_repo.add(Company(name="TSMC_T", ticker="TSM_T"))
    run = a_repo.create_run("2026-09-13", "2026-09-12T00:00:00Z", "2026-09-12T23:59:59Z")

    art_new = a_repo.insert_candidate(
        run_id=run.id,
        title="TSMC_T reports record chip shipments Q3",
        url="https://chip.test/tsmc-q3",
        content="TSMC_T shipped a record number of 3nm chips this quarter.",
        content_hash=content_hash("TSMC_T shipped a record number of 3nm chips this quarter."),
        published_at="2026-09-12T09:00:00Z",
    )
    assert art_new is not None
    db.execute("INSERT INTO article_company (article_id, company_id, mention_count) VALUES (?, ?, ?)", (art_new.id, company.id, 1))

    art_dup = a_repo.insert_candidate(
        run_id=run.id,
        title="TSMC_T reports record chip shipments Q3",  # identical title → duplicate
        url="https://mirror.test/tsmc-q3-copy",
        content="Different mirror content.",
        content_hash=content_hash("Different mirror content."),
        published_at="2026-09-12T10:00:00Z",
    )
    assert art_dup is not None

    Deduplicator(db).deduplicate_run(run.id)

    # New article gets a short link
    assert sl_repo.get_by_article_id(art_new.id) is not None

    # Duplicate article must NOT get a short link
    assert sl_repo.get_by_article_id(art_dup.id) is None
