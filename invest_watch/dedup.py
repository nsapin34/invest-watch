"""Deduplication and event clustering module (4 layers + event grouping).

Implements specifications §7, §9.1, §11 and architecture §4 / §5 / §8 (Step 3).

Layers:
1. URL normalisation (exact match on normalized URL, redirects, tracking parameters stripped).
2. Content hash (exact match on SHA-256 of lowercase whitespace-normalised text).
3. Title similarity (Jaccard on tokenised/stemmed words ≥ title_similarity threshold, e.g. 0.85).
4. Content similarity (MinHash with 128 permutations, Jaccard/cosine ≥ minhash_threshold, e.g. 0.90).
5. Event clustering (temporal window ±event_window_hours, matching company/entities, attached to head event_id).

Statuses assigned:
- 'new': First observation of an original story.
- 'duplicate': Identical URL, identical content_hash, or near-identical title (Jaccard ≥ threshold).
- 'redundant': Same story reprinted/republished without material difference (MinHash ≥ threshold).
- 'updated': Existing article or cluster head with significant content revision/update.
- 'unrelated': Article not matching tracked companies or markets (assigned by classifier or dedup).
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Sequence

from datasketch import MinHash

from .cleaner import content_hash, normalise_whitespace
from .config_manager import DedupSection
from .db_manager import DatabaseManager, utc_now_iso
from .fetcher import normalize_url
from .short_link import ShortLinkRepository

logger = logging.getLogger(__name__)

NUM_PERMUTATIONS = 128
_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_COMMON_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by",
    "from", "about", "as", "into", "like", "through", "after", "over", "between", "out",
    "against", "during", "without", "before", "under", "around", "among", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "et", "ou", "en", "dans",
    "sur", "par", "pour", "avec", "sans", "sous", "est", "sont", "ete", "a", "ont",
})


def tokenize(text: str) -> set[str]:
    """Tokenize text into lowercased terms excluding common stopwords."""
    if not text:
        return set()
    tokens = set()
    for token in _TOKEN_RE.findall(text.lower()):
        if len(token) > 1 and token not in _COMMON_STOPWORDS:
            tokens.add(token)
        elif any("\u4e00" <= ch <= "\u9fff" for ch in token):
            # Keep CJK characters individually or as token
            tokens.add(token)
    return tokens


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity index between two sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def compute_minhash(text: str, num_perm: int = NUM_PERMUTATIONS) -> MinHash:
    """Create a datasketch MinHash object for the given text."""
    mh = MinHash(num_perm=num_perm)
    words = [w for w in normalise_whitespace(text).lower().split() if w]
    for w in words:
        mh.update(w.encode("utf-8"))
    return mh


def _parse_iso_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    clean = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


@dataclass
class ArticleFingerprint:
    """In-memory lightweight record for deduplication comparisons."""

    id: int
    run_id: int
    url_normalized: str
    content_hash: str | None
    title: str
    title_tokens: set[str]
    published_at: datetime | None
    collected_at: datetime | None
    status: str
    event_id: int | None
    minhash: MinHash | None
    company_ids: set[int] = field(default_factory=set)


@dataclass
class DedupResult:
    """Result of running deduplication on a single article."""

    article_id: int
    status: str  # 'new', 'duplicate', 'redundant', 'updated', 'unrelated'
    event_id: int | None
    dedup_reason: str | None
    matched_article_id: int | None = None
    similarity_score: float = 0.0


class Deduplicator:
    """4-layer deduplication engine with event clustering."""

    def __init__(
        self,
        db: DatabaseManager,
        config: DedupSection | None = None,
    ) -> None:
        self.db = db
        self.config = config or DedupSection(
            title_similarity=0.85,
            minhash_threshold=0.9,
            event_window_hours=24,
            seen_days=7,
            new_event_lookback_days=14,
        )

    def load_recent_fingerprints(self, lookback_days: int | None = None) -> list[ArticleFingerprint]:
        """Load articles from the last N days into memory for fast comparison."""
        days = lookback_days if lookback_days is not None else self.config.seen_days
        # Use strftime format compatible with SQLite
        rows = self.db.query(
            "SELECT a.id, a.run_id, a.url_normalized, a.content_hash, a.title, a.content, a.excerpt,"
            " a.published_at, a.collected_at, a.status, a.event_id"
            " FROM article a"
            " WHERE a.collected_at >= datetime('now', '-' || ? || ' days') OR a.collected_at IS NULL"
            " ORDER BY a.id ASC",
            (days,),
        )
        fingerprints: list[ArticleFingerprint] = []
        for r in rows:
            text = r["content"] or r["excerpt"] or ""
            c_hash = r["content_hash"] or (content_hash(text) if text else None)
            mh = compute_minhash(text) if text else None
            # Fetch attached companies
            c_rows = self.db.query("SELECT company_id FROM article_company WHERE article_id = ?", (r["id"],))
            comp_ids = {int(cr["company_id"]) for cr in c_rows}

            pub_dt = _parse_iso_dt(r["published_at"])
            col_dt = _parse_iso_dt(r["collected_at"])

            fingerprints.append(
                ArticleFingerprint(
                    id=int(r["id"]),
                    run_id=int(r["run_id"]),
                    url_normalized=r["url_normalized"],
                    content_hash=c_hash,
                    title=r["title"],
                    title_tokens=tokenize(r["title"]),
                    published_at=pub_dt,
                    collected_at=col_dt,
                    status=r["status"],
                    event_id=int(r["event_id"]) if r["event_id"] is not None else None,
                    minhash=mh,
                    company_ids=comp_ids,
                )
            )
        return fingerprints

    def evaluate_article(
        self,
        candidate: ArticleFingerprint,
        existing: Sequence[ArticleFingerprint],
    ) -> DedupResult:
        """Evaluate a candidate against existing articles through all 4 layers + event clustering."""
        # Check if already flagged as unrelated by classifier
        if candidate.status == "unrelated":
            return DedupResult(
                article_id=candidate.id,
                status="unrelated",
                event_id=None,
                dedup_reason="unrelated to tracked companies or markets",
            )

        candidate_time = candidate.published_at or candidate.collected_at or datetime.now(timezone.utc)

        # Layer 1: Normalised URL
        for ref in existing:
            if ref.id == candidate.id:
                continue
            if candidate.url_normalized and ref.url_normalized == candidate.url_normalized:
                return DedupResult(
                    article_id=candidate.id,
                    status="duplicate",
                    event_id=ref.event_id or ref.id,
                    dedup_reason=f"exact normalized URL match with article #{ref.id}",
                    matched_article_id=ref.id,
                    similarity_score=1.0,
                )

        # Layer 2: Content hash (or identical text)
        for ref in existing:
            if ref.id == candidate.id:
                continue
            if (candidate.content_hash and ref.content_hash and candidate.content_hash == ref.content_hash):
                return DedupResult(
                    article_id=candidate.id,
                    status="duplicate",
                    event_id=ref.event_id or ref.id,
                    dedup_reason=f"identical content hash SHA-256 with article #{ref.id}",
                    matched_article_id=ref.id,
                    similarity_score=1.0,
                )

        # Layer 3: Title similarity (Jaccard)
        for ref in existing:
            if ref.id == candidate.id:
                continue
            sim = jaccard_similarity(candidate.title_tokens, ref.title_tokens)
            if sim >= self.config.title_similarity:
                return DedupResult(
                    article_id=candidate.id,
                    status="duplicate",
                    event_id=ref.event_id or ref.id,
                    dedup_reason=f"title similarity Jaccard {sim:.2f} >= {self.config.title_similarity} with article #{ref.id}",
                    matched_article_id=ref.id,
                    similarity_score=round(sim, 3),
                )

        # Layer 4: Content similarity (MinHash)
        if candidate.minhash:
            for ref in existing:
                if ref.id == candidate.id or not ref.minhash:
                    continue
                minhash_sim = candidate.minhash.jaccard(ref.minhash)
                if minhash_sim >= self.config.minhash_threshold:
                    return DedupResult(
                        article_id=candidate.id,
                        status="redundant",
                        event_id=ref.event_id or ref.id,
                        dedup_reason=f"content MinHash similarity {minhash_sim:.2f} >= {self.config.minhash_threshold} with article #{ref.id}",
                        matched_article_id=ref.id,
                        similarity_score=round(minhash_sim, 3),
                    )

        # Layer 5: Event clustering (same company + ±24h time window)
        matched_event_id: int | None = None
        if candidate.company_ids:
            for ref in existing:
                if ref.id == candidate.id:
                    continue
                # Overlap in tracked companies
                if candidate.company_ids & ref.company_ids:
                    ref_time = ref.published_at or ref.collected_at
                    if ref_time:
                        hours_diff = abs((candidate_time - ref_time).total_seconds()) / 3600.0
                        if hours_diff <= self.config.event_window_hours:
                            # Title similarity check (moderate overlap e.g. >= 0.40) or common story context
                            t_sim = jaccard_similarity(candidate.title_tokens, ref.title_tokens)
                            if t_sim >= 0.40:
                                matched_event_id = ref.event_id or ref.id
                                break

        # If no duplicate/redundancy found:
        return DedupResult(
            article_id=candidate.id,
            status="new",
            event_id=matched_event_id or candidate.id,
            dedup_reason=None if matched_event_id is None else f"clustered with event #{matched_event_id}",
            similarity_score=0.0,
        )

    def deduplicate_run(self, run_id: int) -> list[DedupResult]:
        """Run deduplication on all articles in a given run and update their DB status."""
        history = self.load_recent_fingerprints(lookback_days=self.config.seen_days)
        # Separate history into prior articles vs articles of current run
        prior = [fp for fp in history if fp.run_id != run_id]
        current = [fp for fp in history if fp.run_id == run_id]

        results: list[DedupResult] = []
        now = utc_now_iso()

        # In-flight processed set for intra-run dedup
        seen_so_far = list(prior)

        sl_repo = ShortLinkRepository(self.db)

        with self.db.transaction():
            for art in current:
                res = self.evaluate_article(art, seen_so_far)
                results.append(res)

                # Persist result into database
                self.db.execute(
                    "UPDATE article SET status = ?, event_id = ?, dedup_reason = ?, updated_at = ?"
                    " WHERE id = ?",
                    (res.status, res.event_id, res.dedup_reason, now, art.id),
                )

                # Generate a short link for every new (original) article
                if res.status == "new" and art.url_normalized:
                    sl_repo.create(art.url_normalized, article_id=art.id)

                # Update article fingerprint status & event_id and add to seen_so_far
                art.status = res.status
                art.event_id = res.event_id
                seen_so_far.append(art)

        return results
