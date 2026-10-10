"""Daily report generator — Step 5c.

Implements specifications §13.2, §13.3 and architecture §8 (Step 5).

Responsibilities
----------------
- Read the database after steps 5a (article summaries) and 5b (digests) have run.
- Assemble a fully-structured Markdown report conformant with §13.2:
    * Header (period, LLM status, execution summary)
    * Global summary section
    * 🚨 Critical information table
    * By-company digests
    * By-market digests
    * Key articles of the day table (priority: critique → haute → date → score)
    * Unverified information / uncertainties
    * Errors & inaccessible sources
    * Auto-generated footer
- Write ``reports/YYYY-MM-DD.md``
- Write ``reports/YYYY-MM-DD.json`` (structured mirror with short-link map)
- Both writes are atomic (write to temp file, rename) — a partial write never
  leaves a truncated report.
- Idempotent: regenerating for the same run_id overwrites the existing files.

Short links (§13.3)
-------------------
- Display: ``[a12]``
- Markdown link: ``[a12](url_original)``
- JSON key: ``"a12": "url"``
- The short_code stored in ``article.short_code`` is used as-is; the ``[aNN]``
  notation in digest content is kept verbatim (produced by the LLM or the
  degraded heuristic).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .db_manager import DatabaseManager, utc_now_iso

logger = logging.getLogger(__name__)

# Importance ordering for the key-articles table (§13.3 display priority).
_IMPORTANCE_RANK: dict[str, int] = {
    "critique": 0,
    "haute": 1,
    "moyenne": 2,
    "faible": 3,
}


# ── ReportConfig ──────────────────────────────────────────────────────────────


@dataclass
class ReportConfig:
    """Runtime settings for the report generator.

    Attributes:
        reports_dir:   Absolute path to the ``reports/`` directory.
        model_id:      LLM model identifier (for the header line).
        prompt_version: Article-summary prompt version (for the header line).
        timezone_name: IANA timezone name for display (e.g. 'Europe/Paris').
    """

    reports_dir: Path
    model_id: str = "ibm/granite-3-8b-instruct"
    prompt_version: str = "1.0"
    timezone_name: str = "Europe/Paris"


# ── RunContext (data fetched once from the DB) ────────────────────────────────


@dataclass
class _RunContext:
    """All data needed to render a report, loaded from the database."""

    run_id: int
    run_date: str
    period_start: str
    period_end: str
    status: str
    llm_status: str | None
    error_count: int
    started_at: str | None
    finished_at: str | None
    stats_json: str | None

    articles: list[dict[str, Any]] = field(default_factory=list)
    article_summaries: dict[int, dict[str, Any]] = field(default_factory=dict)
    company_digests: list[dict[str, Any]] = field(default_factory=list)
    market_digests: list[dict[str, Any]] = field(default_factory=list)
    global_digest: dict[str, Any] | None = None
    processing_errors: list[dict[str, Any]] = field(default_factory=list)
    short_links: dict[str, str] = field(default_factory=dict)  # short_code → url_original

    # P1 item 10 — honest LLM reporting
    llm_used_count: int = 0          # article summaries with llm_used=1
    llm_degraded_count: int = 0      # article summaries with llm_used=0
    digest_degraded_count: int = 0   # company/market digests with llm_used=0
    top_failing_hosts: list[tuple[str, int]] = field(default_factory=list)  # (host, n)

    @property
    def total_summaries(self) -> int:
        return self.llm_used_count + self.llm_degraded_count

    @property
    def has_degraded_llm(self) -> bool:
        """True when ≥1 article summary (or digest) was produced degraded."""
        return self.llm_degraded_count > 0 or self.digest_degraded_count > 0

    @property
    def effective_llm_status(self) -> str:
        """Status displayed in the header — degraded wins over a stale 'ok'.

        A run whose ``daily_run.llm_status`` says 'ok' but which contains ≥1
        degraded summary must show *degraded* (P1 item 10 — honest report).
        """
        if self.has_degraded_llm and self.llm_status in ("ok", None):
            return "degraded"
        return self.llm_status or "off"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _duration_str(started_at: str | None, finished_at: str | None) -> str:
    """Return a human-readable duration string, e.g. '34 min'."""
    if not started_at or not finished_at:
        return "?"
    from datetime import datetime, timezone
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        t0 = datetime.strptime(started_at, fmt).replace(tzinfo=timezone.utc)
        t1 = datetime.strptime(finished_at, fmt).replace(tzinfo=timezone.utc)
        secs = int((t1 - t0).total_seconds())
        if secs < 60:
            return f"{secs} s"
        return f"{secs // 60} min"
    except ValueError:
        return "?"


def _period_display(period_start: str, period_end: str, timezone_name: str) -> str:
    """Return a formatted period string for the report header."""
    # Trim to YYYY-MM-DD HH:MM
    s = period_start.replace("T", " ").replace("Z", "")[:16]
    e = period_end.replace("T", " ").replace("Z", "")[:16]
    return f"{s} → {e} ({timezone_name})"


def _llm_status_line(llm_status: str | None, model_id: str, prompt_version: str) -> str:
    """Return the header LLM analysis line (§13.2)."""
    if llm_status == "off" or llm_status is None:
        return "**LLM analysis**: disabled"
    icon = "✔" if llm_status == "ok" else "⚠ degraded"
    return f"**LLM analysis**: watsonx.ai ({model_id}, prompt v{prompt_version}) — {icon}"


def _importance_sort_key(article: dict[str, Any]) -> tuple[int, str, float]:
    """Sort key: importance rank → published_at desc → relevance_score desc."""
    rank = _IMPORTANCE_RANK.get(article.get("importance_level") or "moyenne", 2)
    published = article.get("published_at") or ""
    score = float(article.get("relevance_score") or 0.0)
    return (rank, published, -score)


GOOGLE_TRANSLATE_URL = "https://translate.google.com/translate"


def google_translate_url(url: str | None, language: str | None, target: str = "fr") -> str | None:
    """Google Translate page-proxy link for a non-French article, else ``None``.

    No link for French articles, unknown languages, or PDFs (Google's page
    translation does not handle them - HKEX PDFs are fetched in English anyway).
    """
    if not url or not language:
        return None
    if language.lower().split("-")[0] == target:
        return None
    if url.lower().split("?", 1)[0].endswith(".pdf"):
        return None
    from urllib.parse import quote

    return f"{GOOGLE_TRANSLATE_URL}?sl=auto&tl={target}&hl={target}&u={quote(url, safe='')}"


def _translate_md(url: str | None, language: str | None) -> str:
    """`` · [FR](...)`` suffix for Markdown cells, or an empty string."""
    link = google_translate_url(url, language)
    return f" · [FR]({link})" if link else ""


def _short_link_md(short_code: str, url: str | None) -> str:
    """Return ``[aNN](url)`` if url available, else ``[aNN]``."""
    if url:
        return f"[{short_code}]({url})"
    return f"[{short_code}]"


def _company_label(article: dict[str, Any]) -> str:
    names = article.get("company_names") or ""
    return names.split(",")[0].strip() if names else "—"


def _market_label(article: dict[str, Any]) -> str:
    names = article.get("market_names") or ""
    return names.split(",")[0].strip() if names else "—"


def _category_label(article: dict[str, Any]) -> str:
    names = article.get("category_names") or ""
    return names.split(",")[0].strip() if names else "—"


# ── ReportGenerator ───────────────────────────────────────────────────────────


class ReportGenerator:
    """Reads the database and writes the daily Markdown + JSON report (§13.2).

    Usage::

        cfg = ReportConfig(reports_dir=Path("reports"), model_id="ibm/granite-3-8b-instruct")
        gen = ReportGenerator(db=db_manager, config=cfg)
        md_path, json_path = gen.generate(run_id=7)
    """

    def __init__(self, db: DatabaseManager, config: ReportConfig) -> None:
        self._db = db
        self._cfg = config

    # ── public API ────────────────────────────────────────────────────────

    def generate(self, run_id: int) -> tuple[Path, Path]:
        """Generate and write ``YYYY-MM-DD.md`` + ``YYYY-MM-DD.json``.

        Args:
            run_id: PK of the ``daily_run`` row.

        Returns:
            ``(md_path, json_path)`` — paths of the written files.

        Raises:
            ValueError: If ``run_id`` does not exist in the database.
        """
        ctx = self._load_context(run_id)
        md_text = self._render_markdown(ctx)
        json_obj = self._render_json(ctx)

        if not ctx.articles:
            md_text += (
                "\n---\n\n> ⚠ **Aucun article collecté.** La fenêtre de collection n'a "
                "produit aucun article : sources bloquées par robots.txt, pages "
                "interstitielles (consentement/JS) filtrées, ou clé d'API manquante "
                "sur une source active. Vérifiez `conf/config.toml`, `.env` et les "
                "logs (`invest-watch run -v`).\n"
            )

        self._cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        md_path = self._cfg.reports_dir / f"{ctx.run_date}.md"
        json_path = self._cfg.reports_dir / f"{ctx.run_date}.json"

        _atomic_write(md_path, md_text, encoding="utf-8")
        _atomic_write(json_path, json.dumps(json_obj, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(
            "Report written: %s  %s  (%d articles)",
            md_path.name,
            json_path.name,
            len(ctx.articles),
        )
        return md_path, json_path

    # ── data loading ──────────────────────────────────────────────────────

    def _load_context(self, run_id: int) -> _RunContext:
        """Load everything needed to render the report from the database."""
        run_row = self._db.query_one("SELECT * FROM daily_run WHERE id = ?", (run_id,))
        if run_row is None:
            raise ValueError(f"daily_run #{run_id} not found")

        ctx = _RunContext(
            run_id=run_id,
            run_date=run_row["run_date"],
            period_start=run_row["period_start"],
            period_end=run_row["period_end"],
            status=run_row["status"],
            llm_status=run_row["llm_status"],
            error_count=int(run_row["error_count"] or 0),
            started_at=run_row["started_at"],
            finished_at=run_row["finished_at"],
            stats_json=run_row["stats_json"],
        )

        # Articles (with company/market/category names via GROUP_CONCAT)
        ctx.articles = [
            dict(r)
            for r in self._db.query(
                "SELECT a.id, a.short_code, COALESCE(a.title_fr, a.title) AS title, a.url_original, a.published_at,"
                "       a.language, a.relevance_score, a.importance_level,"
                "       a.requires_human_check, a.status,"
                "       GROUP_CONCAT(DISTINCT c.name)    AS company_names,"
                "       GROUP_CONCAT(DISTINCT m.name)    AS market_names,"
                "       GROUP_CONCAT(DISTINCT cat.name)  AS category_names,"
                "       src.name AS source_name"
                " FROM article a"
                " LEFT JOIN article_company ac  ON ac.article_id = a.id"
                " LEFT JOIN company c            ON c.id = ac.company_id"
                " LEFT JOIN article_market am    ON am.article_id = a.id"
                " LEFT JOIN market m             ON m.id = am.market_id"
                " LEFT JOIN article_category acat ON acat.article_id = a.id"
                " LEFT JOIN category cat          ON cat.id = acat.category_id"
                " LEFT JOIN source src            ON src.id = a.source_id"
                " WHERE a.run_id = ?"
                "   AND a.status NOT IN ('duplicate', 'redundant', 'unrelated')"
                " GROUP BY a.id",
                (run_id,),
            )
        ]

        # Article summaries keyed by article_id
        for row in self._db.query(
            "SELECT ref_id, content, json_payload, model_id, prompt_version, llm_used"
            " FROM summary WHERE run_id = ? AND type = 'article'",
            (run_id,),
        ):
            ctx.article_summaries[int(row["ref_id"])] = dict(row)

        # Company digests
        ctx.company_digests = [
            dict(r)
            for r in self._db.query(
                "SELECT s.ref_id, s.content, s.llm_used, s.validation_status,"
                "       c.name AS ref_name, c.country_code"
                " FROM summary s"
                " JOIN company c ON c.id = s.ref_id"
                " WHERE s.run_id = ? AND s.type = 'company'"
                " ORDER BY c.name",
                (run_id,),
            )
        ]

        # Market digests
        ctx.market_digests = [
            dict(r)
            for r in self._db.query(
                "SELECT s.ref_id, s.content, s.llm_used, s.validation_status,"
                "       m.name AS ref_name, m.type AS market_type"
                " FROM summary s"
                " JOIN market m ON m.id = s.ref_id"
                " WHERE s.run_id = ? AND s.type = 'market'"
                " ORDER BY m.name",
                (run_id,),
            )
        ]

        # Global digest
        global_row = self._db.query_one(
            "SELECT content, llm_used, validation_status"
            " FROM summary WHERE run_id = ? AND type = 'global' AND ref_id IS NULL",
            (run_id,),
        )
        ctx.global_digest = dict(global_row) if global_row else None

        # Processing errors
        ctx.processing_errors = [
            dict(r)
            for r in self._db.query(
                "SELECT pe.step, pe.error_type, pe.message,"
                "       s.name AS source_name, pe.article_id"
                " FROM processing_error pe"
                " LEFT JOIN source s ON s.id = pe.source_id"
                " WHERE pe.run_id = ? AND pe.status != 'resolved'"
                " ORDER BY pe.created_at",
                (run_id,),
            )
        ]

        # Short links: short_code → url_original
        for row in self._db.query(
            "SELECT short_code, url_original FROM short_link"
            " WHERE article_id IN (SELECT id FROM article WHERE run_id = ?)",
            (run_id,),
        ):
            ctx.short_links[row["short_code"]] = row["url_original"]

        # ── P1 item 10 — honest LLM counters ─────────────────────────────
        used = 0
        degraded = 0
        for summary in ctx.article_summaries.values():
            if summary.get("llm_used"):
                used += 1
            else:
                degraded += 1
        ctx.llm_used_count = used
        ctx.llm_degraded_count = degraded
        ctx.digest_degraded_count = sum(
            1 for d in [*ctx.company_digests, *ctx.market_digests]
            if not d.get("llm_used")
        )
        ctx.top_failing_hosts = _top_failing_hosts(ctx.processing_errors)

        return ctx

    # ── Markdown renderer ─────────────────────────────────────────────────

    def _render_markdown(self, ctx: _RunContext) -> str:
        parts: list[str] = []

        # ── Header ────────────────────────────────────────────────────────
        parts.append(f"# invest-watch — Report of {ctx.run_date}\n")
        parts.append(
            f"**Period covered**: {_period_display(ctx.period_start, ctx.period_end, self._cfg.timezone_name)}"
        )
        # P1 item 10 — the LLM line must say *degraded* when ≥1 summary was
        # degraded, even if daily_run.llm_status still says 'ok'.
        parts.append(_llm_status_line(ctx.effective_llm_status, self._cfg.model_id, self._cfg.prompt_version))
        if ctx.total_summaries:
            parts.append(
                f"**LLM usage**: {ctx.llm_used_count}/{ctx.total_summaries} summaries via watsonx.ai"
                f" · {ctx.llm_degraded_count} degraded (llm_used=0)"
                + (f" · {ctx.digest_degraded_count} degraded digest(s)" if ctx.digest_degraded_count else "")
            )
        if ctx.llm_degraded_count > 0 or ctx.digest_degraded_count > 0:
            parts.append(
                f"> ⚠ {ctx.llm_degraded_count + ctx.digest_degraded_count} degraded output(s) —"
                f" retry with `invest-watch report --retry-degraded` (or"
                f" `invest-watch run --resume {ctx.run_id} --retry-degraded`)."
            )
        duration = _duration_str(ctx.started_at, ctx.finished_at)
        error_label = f"{ctx.error_count} minor error{'s' if ctx.error_count != 1 else ''}" if ctx.error_count else "no errors"
        parts.append(f"**Execution**: run #{ctx.run_id} · {ctx.status} · {duration} · {error_label}")
        parts.append("")

        # ── Global summary ─────────────────────────────────────────────────
        parts.append(f"## Global summary of {ctx.run_date}\n")
        if ctx.global_digest:
            parts.append(ctx.global_digest["content"])
        else:
            parts.append("*(No global summary available for this run.)*")
        parts.append("")

        # ── Critical information table ─────────────────────────────────────
        critical = [
            a for a in ctx.articles
            if (a.get("importance_level") or "") == "critique"
        ]
        parts.append("## 🚨 Critical information\n")
        if critical:
            parts.append("| Company | Title | Impact (horizon) | Source | Check |")
            parts.append("|---|---|---|---|---|")
            for a in sorted(critical, key=_importance_sort_key):
                summary = ctx.article_summaries.get(int(a["id"]))
                impact_str = _extract_impact(summary)
                sc = a["short_code"]
                url = a.get("url_original") or ctx.short_links.get(sc, "")
                link = _short_link_md(sc, url) + _translate_md(url, a.get("language"))
                check = "✔" if a.get("requires_human_check") else "—"
                company = _md_cell(_company_label(a))
                title = _md_cell(a["title"])
                parts.append(f"| {company} | {title} | {impact_str} | {link} | {check} |")
        else:
            parts.append("*(No critical information for this run.)*")
        parts.append("")

        # ── By company ────────────────────────────────────────────────────
        parts.append("## By company\n")
        if ctx.company_digests:
            for d in ctx.company_digests:
                parts.append(d["content"])
                parts.append("")
        else:
            parts.append("*(No company digests for this run.)*\n")

        # ── By market ─────────────────────────────────────────────────────
        parts.append("## By market\n")
        if ctx.market_digests:
            for d in ctx.market_digests:
                parts.append(d["content"])
                parts.append("")
        else:
            parts.append("*(No market digests for this run.)*\n")

        # ── Key articles table ─────────────────────────────────────────────
        parts.append("## Key articles of the day\n")
        sorted_articles = sorted(ctx.articles, key=_importance_sort_key)
        if sorted_articles:
            parts.append("| # | Date | Company | Market | Cat. | Score | Title | Source |")
            parts.append("|---|---|---|---|---|---|---|---|")
            for a in sorted_articles:
                sc = a["short_code"]
                url = a.get("url_original") or ctx.short_links.get(sc, "")
                link = _short_link_md(sc, url)
                date_str = (a.get("published_at") or "")[:10]
                score = f"{float(a.get('relevance_score') or 0):.2f}"
                company = _md_cell(_company_label(a))
                market = _md_cell(_market_label(a))
                cat = _md_cell(_category_label(a))
                title_short = _md_cell((a["title"] or "")[:80])
                source = _md_cell(a.get("source_name") or "—")
                parts.append(
                    f"| {sc} | {date_str} | {company} | {market} | {cat} | {score} | {title_short} | [{source}]({url}){_translate_md(url, a.get('language'))} |"
                )
        else:
            parts.append("*(No articles for this run.)*")
        parts.append("")

        # ── Unverified information / uncertainties ─────────────────────────
        parts.append("## Unverified information / uncertainties\n")
        unverified: list[str] = []
        for a in ctx.articles:
            summary = ctx.article_summaries.get(int(a["id"]))
            if not summary or not summary.get("json_payload"):
                continue
            try:
                payload = json.loads(summary["json_payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            sc = a["short_code"]
            for item in (payload.get("incertitudes_ou_conflits") or []):
                unverified.append(f"- ❓ {item} [{sc}]")
            for item in (payload.get("note_contradictions") or []):
                unverified.append(f"- ⚠ {item} [{sc}]")

        if unverified:
            parts.extend(unverified)
        else:
            parts.append("*(No uncertainties reported for this run.)*")
        parts.append("")

        # ── Errors & inaccessible sources ─────────────────────────────────
        parts.append("## Errors & inaccessible sources\n")
        if ctx.processing_errors:
            for err in ctx.processing_errors:
                src = err.get("source_name") or err.get("step") or "unknown"
                msg = err.get("message") or err.get("error_type") or ""
                parts.append(f"- [{err.get('error_type') or 'unknown'}] {src}: {msg}")
            if ctx.top_failing_hosts:
                hosts_str = ", ".join(f"{h} ({n})" for h, n in ctx.top_failing_hosts)
                parts.append("")
                parts.append(f"**Top failing hosts**: {hosts_str}")
        else:
            parts.append("*(No errors recorded for this run.)*")
        parts.append("")

        # ── Footer ────────────────────────────────────────────────────────
        parts.append("> Auto-generated report — every fact is verifiable in its original source.")

        return "\n".join(parts)

    # ── JSON renderer ─────────────────────────────────────────────────────

    def _render_json(self, ctx: _RunContext) -> dict[str, Any]:
        """Build the structured JSON mirror of the Markdown report."""

        # Short-link map: {"a12": "https://..."}
        short_link_map: dict[str, str] = {}
        for a in ctx.articles:
            sc = a["short_code"]
            url = a.get("url_original") or ctx.short_links.get(sc, "")
            if url:
                short_link_map[sc] = url

        # Articles
        articles_json: list[dict[str, Any]] = []
        for a in sorted(ctx.articles, key=_importance_sort_key):
            summary = ctx.article_summaries.get(int(a["id"]))
            payload: dict[str, Any] = {}
            if summary and summary.get("json_payload"):
                try:
                    payload = json.loads(summary["json_payload"])
                except (json.JSONDecodeError, TypeError):
                    payload = {}
            sc = a["short_code"]
            articles_json.append({
                "id": sc,
                "article_id": int(a["id"]),
                "title": a.get("title"),
                "url": a.get("url_original"),
                "translate_url": google_translate_url(a.get("url_original"), a.get("language")),
                "published_at": a.get("published_at"),
                "source": a.get("source_name"),
                "company": _company_label(a),
                "market": _market_label(a),
                "category": _category_label(a),
                "relevance_score": float(a.get("relevance_score") or 0),
                "importance_level": a.get("importance_level"),
                "requires_human_check": bool(a.get("requires_human_check")),
                "llm_used": bool(summary.get("llm_used")) if summary else False,
                "model_id": summary.get("model_id") if summary else None,
                "prompt_version": summary.get("prompt_version") if summary else None,
                "faits": payload.get("faits", []),
                "incertitudes_ou_conflits": payload.get("incertitudes_ou_conflits", []),
                "consequences_potentielles": payload.get("consequences_potentielles", []),
                "note_contradictions": payload.get("note_contradictions", []),
                "verification_humaine_requise": payload.get("verification_humaine_requise", False),
            })

        # Company digests
        companies_json: list[dict[str, Any]] = [
            {
                "name": d.get("ref_name"),
                "country_code": d.get("country_code"),
                "content": d.get("content"),
                "llm_used": bool(d.get("llm_used")),
            }
            for d in ctx.company_digests
        ]

        # Market digests
        markets_json: list[dict[str, Any]] = [
            {
                "name": d.get("ref_name"),
                "type": d.get("market_type"),
                "content": d.get("content"),
                "llm_used": bool(d.get("llm_used")),
            }
            for d in ctx.market_digests
        ]

        return {
            "run_id": ctx.run_id,
            "run_date": ctx.run_date,
            "period_start": ctx.period_start,
            "period_end": ctx.period_end,
            "status": ctx.status,
            "llm_status": ctx.effective_llm_status,
            "error_count": ctx.error_count,
            "llm_used_count": ctx.llm_used_count,
            "llm_degraded_count": ctx.llm_degraded_count,
            "digest_degraded_count": ctx.digest_degraded_count,
            "top_failing_hosts": [
                {"host": host, "count": n} for host, n in ctx.top_failing_hosts
            ],
            "generated_at": utc_now_iso(),
            "global_summary": ctx.global_digest["content"] if ctx.global_digest else None,
            "articles": articles_json,
            "companies": companies_json,
            "markets": markets_json,
            "errors": [
                {
                    "step": e.get("step"),
                    "source": e.get("source_name"),
                    "type": e.get("error_type"),
                    "message": e.get("message"),
                }
                for e in ctx.processing_errors
            ],
            "short_links": short_link_map,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _md_cell(text: Any) -> str:
    """Escape a string for a Markdown table cell — ``|`` would break the column.

    P1 item 10: a title like ``Access denied | publisher used Cloudflare``
    used to shift every following cell of the critical table.
    """
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _host_of_message(message: str | None) -> str | None:
    """Extract the hostname from an error message when it holds a URL.

    Collector fetch errors store the URL as ``message`` (P1 item 7), so the
    per-host aggregation can group ``http_403`` failures per publisher.
    """
    raw = (message or "").strip()
    for candidate in raw.split():
        if candidate.startswith(("http://", "https://")):
            host = urlsplit(candidate).netloc.lower()
            if host:
                return host
    return None


def _top_failing_hosts(
    processing_errors: list[dict[str, Any]],
    top_n: int = 5,
) -> list[tuple[str, int]]:
    """Group unresolved errors by host and return the worst hosts first."""
    counts: dict[str, int] = {}
    for err in processing_errors:
        host = _host_of_message(err.get("message"))
        if host is None:
            continue
        counts[host] = counts.get(host, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:top_n]


def _extract_impact(summary: dict[str, Any] | None) -> str:
    """Extract a concise impact string from the article summary JSON payload.

    P1 item 10 — degraded summaries (``llm_used=0``) carry a heuristic
    payload without a trustworthy consequence block: they display ``—``
    instead of an empty or misleading ``unknown`` cell.
    """
    if not summary or not summary.get("json_payload"):
        return "—"
    if "llm_used" in summary and not summary.get("llm_used"):
        # Degraded heuristic summary (P1 item 10) — no honest impact can be
        # claimed: an explicit marker beats an empty or invented cell.  Only
        # explicit ``llm_used=0`` rows are flagged; callers that omit the key
        # keep the plain ``—`` placeholder.
        return "— (dégradé)"
    try:
        payload = json.loads(summary["json_payload"])
    except (json.JSONDecodeError, TypeError):
        return "—"
    cons = payload.get("consequences_potentielles") or []
    if not cons:
        return "—"
    first = cons[0]
    impact = first.get("impact") or "—"
    horizon = first.get("horizon") or "—"
    conf = first.get("confidence") or "—"
    return _md_cell(f"{impact} ({horizon}), conf. {conf}")


def _atomic_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically (temp file + rename)."""
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
