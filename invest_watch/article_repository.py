"""Persistence for ``daily_run`` rows and raw article candidates (Step 2).

Classification, dedup statuses and short-link generation belong to Step 3;
this repository only stores harvested pages so ``invest-watch run`` has a
durable list of candidates. Writes go exclusively through ``db_manager``.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from .db_manager import DatabaseManager, utc_now_iso
from .exceptions import NotFoundError
from .fetcher import normalize_url


@dataclass
class DailyRun:
    id: int
    run_date: str
    period_start: str
    period_end: str
    status: str
    error_count: int = 0
    stats_json: str | None = None
    trigger: str | None = "manual"


@dataclass
class Article:
    id: int
    run_id: int
    title: str
    url_original: str
    url_normalized: str
    short_code: str
    status: str
    source_id: int | None = None
    guid: str | None = None
    author: str | None = None
    language: str | None = None
    published_at: str | None = None
    date_certaine: bool = False
    content: str | None = None
    excerpt: str | None = None
    content_hash: str | None = None
    title_hash: str | None = None
    word_count: int | None = None
    collected_at: str | None = None


def _short_code_for(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return digest[:8]


class ArticleRepository:
    """Insert / lookup raw candidates and their parent ``daily_run``."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def create_run(
        self,
        run_date: str,
        period_start: str,
        period_end: str,
        *,
        trigger: str = "manual",
        llm_status: str = "off",
    ) -> DailyRun:
        now = utc_now_iso()
        existing = self.db.query_one("SELECT * FROM daily_run WHERE run_date = ?", (run_date,))
        if existing is not None:
            self.db.execute(
                "UPDATE daily_run SET status = 'running', period_start = ?, period_end = ?,"
                " started_at = ?, trigger = ? WHERE id = ?",
                (period_start, period_end, now, trigger, existing["id"]),
            )
            return DailyRun(
                id=existing["id"],
                run_date=run_date,
                period_start=period_start,
                period_end=period_end,
                status="running",
                trigger=trigger,
            )
        cur = self.db.execute(
            "INSERT INTO daily_run (run_date, period_start, period_end, status, started_at,"
            " error_count, llm_status, trigger, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_date, period_start, period_end, "running", now, 0, llm_status, trigger, now),
        )
        return DailyRun(
            id=int(cur.lastrowid),
            run_date=run_date,
            period_start=period_start,
            period_end=period_end,
            status="running",
            trigger=trigger,
        )

    def finish_run(self, run_id: int, *, status: str, error_count: int, stats_json: str | None) -> None:
        self.db.execute(
            "UPDATE daily_run SET status = ?, finished_at = ?, error_count = ?, stats_json = ?"
            " WHERE id = ?",
            (status, utc_now_iso(), error_count, stats_json, run_id),
        )

    def get_run(self, run_id: int) -> DailyRun | None:
        row = self.db.query_one("SELECT * FROM daily_run WHERE id = ?", (run_id,))
        if row is None:
            return None
        return DailyRun(
            id=row["id"],
            run_date=row["run_date"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            status=row["status"],
            error_count=row["error_count"],
            stats_json=row["stats_json"],
            trigger=row["trigger"],
        )

    def insert_candidate(
        self,
        *,
        run_id: int,
        title: str,
        url: str,
        source_id: int | None = None,
        guid: str | None = None,
        author: str | None = None,
        language: str | None = None,
        published_at: str | None = None,
        date_certaine: bool = False,
        content: str | None = None,
        excerpt: str | None = None,
        content_hash: str | None = None,
        word_count: int | None = None,
        title_fr: str | None = None,
        content_fr: str | None = None,
    ) -> Article | None:
        """Idempotent insert. Returns ``None`` when the URL already exists."""
        url_original = url.strip()
        url_normalized = normalize_url(url_original)
        now = utc_now_iso()
        code = _short_code_for(url_normalized)
        t_hash = hashlib.sha256(title.strip().lower().encode("utf-8")).hexdigest() if title else None
        guid_value = guid.strip() if guid else None
        try:
            with self.db.transaction():
                cur = self.db.execute(
                    "INSERT INTO article (run_id, source_id, guid, url_original, url_normalized,"
                    " title, author, language, published_at, date_certaine, collected_at, content,"
                    " excerpt, content_hash, title_hash, short_code, status, word_count,"
                    " first_seen_run_id, created_at, updated_at, title_fr, content_fr)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        source_id,
                        guid_value,
                        url_original,
                        url_normalized,
                        title.strip(),
                        author,
                        language,
                        published_at,
                        int(bool(date_certaine)),
                        now,
                        content,
                        excerpt,
                        content_hash,
                        t_hash,
                        code,
                        "new",
                        word_count,
                        run_id,
                        now,
                        now,
                        title_fr,
                        content_fr,
                    ),
                )
                article_id = int(cur.lastrowid)
                self.db.execute(
                    "INSERT INTO article_fts (article_id, title, excerpt, content)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        article_id,
                        # Original + French translation (v008): Chinese articles
                        # become searchable with French keywords.
                        " ".join(p for p in ((title or "").strip(), title_fr or "") if p),
                        excerpt or "",
                        "\n".join(p for p in (content or "", content_fr or "") if p),
                    ),
                )
        except sqlite3.IntegrityError:
            return None
        return Article(
            id=article_id,
            run_id=run_id,
            title=title.strip(),
            url_original=url_original,
            url_normalized=url_normalized,
            short_code=code,
            status="new",
            source_id=source_id,
            guid=guid_value,
            author=author,
            language=language,
            published_at=published_at,
            date_certaine=date_certaine,
            content=content,
            excerpt=excerpt,
            content_hash=content_hash,
            title_hash=t_hash,
            word_count=word_count,
            collected_at=now,
        )

    def list_for_run(self, run_id: int) -> list[Article]:
        rows = self.db.query(
            "SELECT * FROM article WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [self._from_row(row) for row in rows]

    def count_for_run(self, run_id: int) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM article WHERE run_id = ?", (run_id,))
        return int(row["n"]) if row else 0

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Article:
        return Article(
            id=row["id"],
            run_id=row["run_id"],
            title=row["title"],
            url_original=row["url_original"],
            url_normalized=row["url_normalized"],
            short_code=row["short_code"],
            status=row["status"],
            source_id=row["source_id"],
            guid=row["guid"],
            author=row["author"],
            language=row["language"],
            published_at=row["published_at"],
            date_certaine=bool(row["date_certaine"]),
            content=row["content"],
            excerpt=row["excerpt"],
            content_hash=row["content_hash"],
            title_hash=row["title_hash"],
            word_count=row["word_count"],
            collected_at=row["collected_at"],
        )


def require_run(repo: ArticleRepository, run_id: int) -> DailyRun:
    run = repo.get_run(run_id)
    if run is None:
        raise NotFoundError(f"daily_run #{run_id} not found")
    return run
