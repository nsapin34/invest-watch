"""Article summarizer — Steps 5a & 5b.

Implements specifications §8.1–§8.8 and architecture §8 (Step 5).

Step 5a — Article summaries
----------------------------
- Build the LLM prompt from an article row (§8.3).
- Call the LLM via :class:`~invest_watch.llm_client.WatsonxLLMClient`
  (or any stub that shares the same interface).
- Validate the JSON payload against the strict schema (§8.4):
  enumerations, score bounds [0..1], mandatory fields, max lengths.
- Fall back to a deterministic heuristic summary when the LLM is
  unavailable or produces invalid output (§8.8 degraded mode).
- Respect the per-run call cap (``max_calls_per_run``, §8.6).
- Enforce the ``max_input_chars`` truncation rule (§8.6).
- Persist the ``summary`` row (type='article', ref_id=article_id) in a
  single upsert — idempotent if called twice for the same article/run.

Step 5b — Company / market / global digests
--------------------------------------------
- :class:`DigestSummarizer` produces three digest levels:
    * **company** (type='company', ref_id=company_id) — one per tracked company,
      from the article summaries of articles linked to that company.
    * **market** (type='market', ref_id=market_id) — one per tracked market,
      from article summaries of articles linked to that market.
    * **global** (type='global', ref_id=NULL) — one per run, synthesised
      from all company and market digests; adds a "Critical information" section.
- Each digest is **Markdown** (not JSON), with mandatory ``[aNN]`` citations
  after every assertion (§8.3, §13.3).
- Digests reuse existing ``summary`` rows — the LLM is never called again for
  the same article content (§8.6).
- Full degraded mode: if the LLM is unavailable the digest is assembled from
  raw ``summary.content`` bullet lists, marked ``llm_used=False`` (§8.8).
- Idempotent upsert on ``(run_id, type, ref_id)`` — safe for ``--resume``.

Key constants
-------------
PROMPT_VERSION : str
    Bumped whenever the article-summary prompt template changes.
    Stored in every ``summary.prompt_version`` column for traceability.
DIGEST_PROMPT_VERSION : str
    Independent version counter for digest prompts.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .db_manager import DatabaseManager, utc_now_iso
from .llm_client import LLMDegradedError

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

PROMPT_VERSION = "1.0"

# Canonical category values accepted in the LLM JSON response (§8.3 schema).
_VALID_CATEGORIES = frozenset(
    [
        "financial_results",
        "corporate_actions",
        "mergers_acquisitions",
        "regulator",
        "product",
        "operations",
        "macro",
        "geopolitical",
        "competitive",
        "sustainability",
        "other",
    ]
)

_VALID_IMPACTS = frozenset(["positive", "negative", "mixed", "unknown"])
_VALID_HORIZONS = frozenset(["court", "moyen", "long", "inconnu"])
_VALID_CONFIDENCES = frozenset(["haute", "moyenne", "basse"])
_VALID_IMPORTANCES = frozenset(["critique", "haute", "moyenne", "faible"])

# Hard limits for string fields — truncated during validation, not rejected.
_MAX_TITLE_CHARS = 300
_MAX_FACT_CHARS = 500
_MAX_CONSEQUENCE_CHARS = 300
_MAX_UNCERTAINTY_CHARS = 300
_MAX_CONTRADICTION_CHARS = 300

# Maximum number of items in list fields.
_MAX_LIST_ITEMS = 20


# ── LLM client protocol (for type-checking stubs and real client alike) ───────


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Minimal interface required by :class:`ArticleSummarizer`."""

    @property
    def is_degraded(self) -> bool: ...  # noqa: D102

    def generate_json(
        self, prompt: str, content_hash: str | None = None
    ) -> dict[str, Any]: ...  # noqa: D102


# ── Configuration ─────────────────────────────────────────────────────────────


@dataclass
class SummarizerConfig:
    """Runtime settings for the article summarizer.

    Built from :class:`~invest_watch.config_manager.LLMSection` by
    :func:`summarizer_config_from_section`.  Kept decoupled so this module
    has no import dependency on ``config_manager``.
    """

    enabled: bool = True
    model_id: str = "ibm/granite-3-8b-instruct"
    prompt_version: str = PROMPT_VERSION
    max_input_chars: int = 8000
    max_calls_per_run: int = 150
    relevance_min: float = 0.5


def summarizer_config_from_section(
    llm_section: Any,
    *,
    relevance_min: float = 0.5,
) -> SummarizerConfig:
    """Build a :class:`SummarizerConfig` from a ``LLMSection`` dataclass.

    Args:
        llm_section:   :class:`~invest_watch.config_manager.LLMSection` instance.
        relevance_min: Minimum relevance_score to trigger an LLM call (§8.6).
                       Taken from ``scoring.relevance_min`` in the config.
    """
    return SummarizerConfig(
        enabled=llm_section.enabled,
        model_id=llm_section.model,
        prompt_version=PROMPT_VERSION,
        max_input_chars=llm_section.max_input_chars,
        max_calls_per_run=llm_section.max_calls_per_run,
        relevance_min=relevance_min,
    )


# ── ArticleSummary dataclass (structured payload) ─────────────────────────────


@dataclass
class ArticleSummary:
    """Structured result of summarising one article.

    Mirrors the JSON schema from §8.3 plus persistence metadata.

    Attributes:
        article_id:         FK to ``article.id``.
        run_id:             FK to ``daily_run.id``.
        llm_used:           True → LLM call succeeded; False → degraded heuristic.
        validation_status:  'ok' | 'degraded' | 'invalid'.
        model_id:           Model identifier (e.g. 'ibm/granite-3-8b-instruct').
        prompt_version:     Template version stored for traceability.
        langue_resume:      Language of the produced summary.
        titre_resume:       Short summary title (≤ 300 chars).
        categorie:          Thematic category (one of ``_VALID_CATEGORIES``).
        compagnies_concernees: Company names mentioned in the article.
        marches_couverts:   Market/sector names mentioned.
        faits:              Bullet list of reported facts.
        consequences_potentielles: Structured impact items.
        incertitudes_ou_conflits:  Uncertainties or conflicting information.
        verification_humaine_requise: Whether human verification is recommended.
        importance:         Importance level from the LLM ('critique'/'haute'/…).
        pertinence:         LLM relevance score [0.0, 1.0] (clamped, not used as-is).
        note_contradictions: Flagged contradictions.
        content_hash:       SHA-256 of the article content (cache key).
        db_id:              ``summary.id`` after persistence; ``None`` before.
    """

    article_id: int
    run_id: int
    llm_used: bool
    validation_status: str  # 'ok' | 'degraded' | 'invalid'
    model_id: str
    prompt_version: str

    langue_resume: str = "fr"
    titre_resume: str = ""
    categorie: str = "other"
    compagnies_concernees: list[str] = field(default_factory=list)
    marches_couverts: list[str] = field(default_factory=list)
    faits: list[str] = field(default_factory=list)
    consequences_potentielles: list[dict[str, Any]] = field(default_factory=list)
    incertitudes_ou_conflits: list[str] = field(default_factory=list)
    verification_humaine_requise: bool = False
    importance: str = "moyenne"
    pertinence: float = 0.0
    note_contradictions: list[str] = field(default_factory=list)

    content_hash: str | None = None
    db_id: int | None = None

    def to_json_payload(self) -> str:
        """Serialise the structured payload to a compact JSON string for storage."""
        return json.dumps(
            {
                "langue_resume": self.langue_resume,
                "titre_resume": self.titre_resume,
                "categorie": self.categorie,
                "compagnies_concernees": self.compagnies_concernees,
                "marches_couverts": self.marches_couverts,
                "faits": self.faits,
                "consequences_potentielles": self.consequences_potentielles,
                "incertitudes_ou_conflits": self.incertitudes_ou_conflits,
                "verification_humaine_requise": self.verification_humaine_requise,
                "importance": self.importance,
                "pertinence": self.pertinence,
                "note_contradictions": self.note_contradictions,
            },
            ensure_ascii=False,
        )

    def to_text_content(self) -> str:
        """Human-readable text stored in ``summary.content``."""
        lines = [f"**{self.titre_resume}**"]
        if self.faits:
            lines.append("\nFaits :")
            for f in self.faits:
                lines.append(f"- {f}")
        if self.incertitudes_ou_conflits:
            lines.append("\nIncertitudes :")
            for u in self.incertitudes_ou_conflits:
                lines.append(f"- {u}")
        lines.append(
            f"\n[{self.model_id} · prompt v{self.prompt_version} · "
            f"{'LLM' if self.llm_used else 'dégradé'}]"
        )
        return "\n".join(lines)


# ── Prompt builder ────────────────────────────────────────────────────────────


def build_article_prompt(
    *,
    title: str,
    text: str,
    language: str,
    published_at: str | None,
    company_names: list[str],
    market_names: list[str],
    max_input_chars: int = 8000,
) -> str:
    """Build the LLM prompt for article summarisation (§8.3 template).

    The article text is truncated to ``max_input_chars`` before embedding.
    """
    truncated_text = text[:max_input_chars] if text else ""
    companies_str = ", ".join(company_names) if company_names else "aucune"
    markets_str = ", ".join(market_names) if market_names else "aucun"
    date_str = published_at or "inconnue"

    return (
        "Tu es un analyste factuel. Tu résumes UNIQUEMENT le contenu fourni.\n"
        "CONTEXTE :\n"
        f"- Compagnies suivies : {companies_str}\n"
        f"- Marchés suivis : {markets_str}\n"
        f"TEXTE À ANALYSER (langue : {language}) :\n"
        "--- DÉBUT ---\n"
        f"{title}\n"
        f"{date_str}\n"
        f"{truncated_text}\n"
        "--- FIN ---\n"
        "Produis UNIQUEMENT du JSON valide selon ce schéma :\n"
        "{\n"
        '  "langue_resume": "fr",\n'
        '  "titre_resume": "...",\n'
        '  "categorie": "financial_results|corporate_actions|mergers_acquisitions'
        "|regulator|product|operations|macro|geopolitical|competitive"
        '|sustainability|other",\n'
        '  "compagnies_concernees": ["..."],\n'
        '  "marches_couverts": ["..."],\n'
        '  "faits": ["..."],\n'
        '  "consequences_potentielles": [\n'
        '    {"cible": "...", "impact": "positive|negative|mixed|unknown",\n'
        '     "horizon": "court|moyen|long|inconnu", "confidence": "haute|moyenne|basse"}\n'
        "  ],\n"
        '  "incertitudes_ou_conflits": ["..."],\n'
        '  "verification_humaine_requise": true,\n'
        '  "importance": "critique|haute|moyenne|faible",\n'
        '  "pertinence": 0.0,\n'
        '  "note_contradictions": ["..."]\n'
        "}\n"
        "Regles : ne rien inventer ; si un champ est inconnu, utiliser null ou [] ;\n"
        "les faits doivent être vérifiables dans le texte fourni."
    )


# ── JSON schema validation ─────────────────────────────────────────────────────


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _truncate_str(value: Any, max_chars: int) -> str:
    s = str(value) if value is not None else ""
    return s[:max_chars]


def _str_list(value: Any, max_items: int, max_item_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:max_items]:
        if item is not None:
            result.append(_truncate_str(item, max_item_chars))
    return result


def validate_article_json(payload: dict[str, Any]) -> tuple[bool, ArticleSummary | None, str]:
    """Validate and normalise the raw LLM JSON payload (§8.4 strict schema).

    Returns:
        (ok, summary_or_none, error_message)
        - ok=True, summary filled, error='' on success.
        - ok=False, summary=None, error=<reason> on hard failure.

    Soft violations (value out of enum, score out of bounds) are fixed by
    clamping / defaulting rather than rejected — the ``validation_status``
    will be 'ok' when all fixable, 'invalid' only on structural failures.
    """
    if not isinstance(payload, dict):
        return False, None, "payload is not a dict"

    # ── categorie ──────────────────────────────────────────────────────────
    raw_cat = payload.get("categorie")
    categorie = raw_cat if raw_cat in _VALID_CATEGORIES else "other"

    # ── importance ──────────────────────────────────────────────────────────
    raw_imp = payload.get("importance")
    importance = raw_imp if raw_imp in _VALID_IMPORTANCES else "moyenne"

    # ── pertinence (clamped to [0, 1]) ─────────────────────────────────────
    raw_pert = payload.get("pertinence")
    try:
        pertinence = _clamp(float(raw_pert)) if raw_pert is not None else 0.0
    except (TypeError, ValueError):
        pertinence = 0.0

    # ── verification_humaine_requise ────────────────────────────────────────
    raw_vhr = payload.get("verification_humaine_requise")
    if isinstance(raw_vhr, bool):
        vhr = raw_vhr
    elif isinstance(raw_vhr, int):
        vhr = bool(raw_vhr)
    else:
        vhr = False

    # ── string fields ───────────────────────────────────────────────────────
    langue = _truncate_str(payload.get("langue_resume") or "fr", 10)
    titre = _truncate_str(payload.get("titre_resume") or "", _MAX_TITLE_CHARS)

    # ── list fields ─────────────────────────────────────────────────────────
    compagnies = _str_list(payload.get("compagnies_concernees"), _MAX_LIST_ITEMS, 200)
    marches = _str_list(payload.get("marches_couverts"), _MAX_LIST_ITEMS, 200)
    faits = _str_list(payload.get("faits"), _MAX_LIST_ITEMS, _MAX_FACT_CHARS)
    incertitudes = _str_list(
        payload.get("incertitudes_ou_conflits"), _MAX_LIST_ITEMS, _MAX_UNCERTAINTY_CHARS
    )
    contradictions = _str_list(
        payload.get("note_contradictions"), _MAX_LIST_ITEMS, _MAX_CONTRADICTION_CHARS
    )

    # ── consequences_potentielles (structured list) ─────────────────────────
    raw_cons = payload.get("consequences_potentielles")
    consequences: list[dict[str, Any]] = []
    if isinstance(raw_cons, list):
        for item in raw_cons[:_MAX_LIST_ITEMS]:
            if not isinstance(item, dict):
                continue
            cible = _truncate_str(item.get("cible") or "", 200)
            impact = item.get("impact")
            horizon = item.get("horizon")
            confidence = item.get("confidence")
            consequences.append(
                {
                    "cible": cible,
                    "impact": impact if impact in _VALID_IMPACTS else "unknown",
                    "horizon": horizon if horizon in _VALID_HORIZONS else "inconnu",
                    "confidence": confidence if confidence in _VALID_CONFIDENCES else "basse",
                }
            )

    summary = ArticleSummary(
        article_id=0,  # filled by caller
        run_id=0,      # filled by caller
        llm_used=True,
        validation_status="ok",
        model_id="",   # filled by caller
        prompt_version=PROMPT_VERSION,
        langue_resume=langue,
        titre_resume=titre,
        categorie=categorie,
        compagnies_concernees=compagnies,
        marches_couverts=marches,
        faits=faits,
        consequences_potentielles=consequences,
        incertitudes_ou_conflits=incertitudes,
        verification_humaine_requise=vhr,
        importance=importance,
        pertinence=round(pertinence, 4),
        note_contradictions=contradictions,
    )
    return True, summary, ""


# ── Degraded heuristic fallback ───────────────────────────────────────────────


def _degraded_summary(
    *,
    title: str,
    excerpt: str | None,
    article_id: int,
    run_id: int,
    model_id: str,
    content_hash: str | None,
) -> ArticleSummary:
    """Produce a minimal heuristic summary when the LLM is unavailable (§8.8).

    Uses the article title as ``titre_resume`` and the excerpt (first
    ~500 chars) as the single fact.  Marked ``llm_used=False``,
    ``validation_status='degraded'``.
    """
    first_fact = (excerpt or "")[:_MAX_FACT_CHARS].strip() or title
    return ArticleSummary(
        article_id=article_id,
        run_id=run_id,
        llm_used=False,
        validation_status="degraded",
        model_id=model_id,
        prompt_version=PROMPT_VERSION,
        titre_resume=title[:_MAX_TITLE_CHARS],
        faits=[first_fact] if first_fact else [],
        content_hash=content_hash,
    )


# ── ArticleSummarizer ─────────────────────────────────────────────────────────


class ArticleSummarizer:
    """Summarises individual articles using the LLM or a degraded heuristic.

    The summarizer is the sole owner of the per-run call counter.  It does
    NOT own the LLM client (injected) so tests can substitute a stub.

    Usage::

        cfg = summarizer_config_from_section(app_config.llm, relevance_min=0.5)
        summarizer = ArticleSummarizer(db=db, llm=llm_client, config=cfg)
        summary = summarizer.summarise_article(article_id=42, run_id=7)
        # summary is persisted in the database and returned.
    """

    def __init__(
        self,
        db: DatabaseManager,
        llm: LLMClientProtocol,
        config: SummarizerConfig,
    ) -> None:
        self._db = db
        self._llm = llm
        self._cfg = config
        self._call_count = 0  # per-run counter

    # ── public API ────────────────────────────────────────────────────────

    @property
    def call_count(self) -> int:
        """Number of successful LLM calls made in this run."""
        return self._call_count

    def reset_run(self) -> None:
        """Reset the per-run call counter (called by the orchestrator at run start)."""
        self._call_count = 0

    def summarise_article(self, article_id: int, run_id: int) -> ArticleSummary:
        """Summarise one article and persist the result in ``summary``.

        Decision tree (§8.6, §8.8):
        1. If the article's ``relevance_score < relevance_min``, skip LLM →
           degrade immediately.
        2. If the per-run cap is reached, degrade.
        3. If LLM is disabled / circuit-breaker open, degrade.
        4. If ``content`` exceeds ``max_input_chars``, degrade (no chunking in V1).
        5. Otherwise call the LLM; if it fails (LLMDegradedError), degrade.
        6. Validate the JSON payload; if validation hard-fails, degrade.
        7. Persist idempotently and return.

        Args:
            article_id: PK of the article row to summarise.
            run_id:     PK of the current daily_run.

        Returns:
            Persisted :class:`ArticleSummary` with ``db_id`` filled.

        Raises:
            ValueError: If ``article_id`` does not exist in the database.
        """
        row = self._db.query_one(
            "SELECT a.id, a.run_id, a.title, a.content, a.excerpt,"
            " a.language, a.published_at, a.content_hash, a.relevance_score,"
            " GROUP_CONCAT(DISTINCT c.name) AS company_names,"
            " GROUP_CONCAT(DISTINCT m.name) AS market_names"
            " FROM article a"
            " LEFT JOIN article_company ac ON ac.article_id = a.id"
            " LEFT JOIN company c ON c.id = ac.company_id"
            " LEFT JOIN article_market am ON am.article_id = a.id"
            " LEFT JOIN market m ON m.id = am.market_id"
            " WHERE a.id = ?"
            " GROUP BY a.id",
            (article_id,),
        )
        if row is None:
            raise ValueError(f"article #{article_id} not found")

        title = row["title"] or ""
        content = row["content"] or ""
        excerpt = row["excerpt"] or ""
        language = row["language"] or "fr"
        published_at = row["published_at"]
        content_hash = row["content_hash"]
        relevance_score = float(row["relevance_score"] or 0.0)
        company_names = [n for n in (row["company_names"] or "").split(",") if n]
        market_names = [n for n in (row["market_names"] or "").split(",") if n]

        # ── decision: can we call the LLM? ────────────────────────────────
        degraded_reason: str | None = None

        if not self._cfg.enabled:
            degraded_reason = "LLM disabled"
        elif self._llm.is_degraded:
            degraded_reason = "circuit-breaker open"
        elif relevance_score < self._cfg.relevance_min:
            degraded_reason = f"relevance {relevance_score:.2f} < min {self._cfg.relevance_min:.2f}"
        elif self._call_count >= self._cfg.max_calls_per_run:
            degraded_reason = f"per-run cap reached ({self._cfg.max_calls_per_run})"
        elif len(content) > self._cfg.max_input_chars:
            degraded_reason = f"content length {len(content)} > max {self._cfg.max_input_chars}"

        if degraded_reason is not None:
            logger.debug(
                "article #%d: degraded mode — %s", article_id, degraded_reason
            )
            summary = _degraded_summary(
                title=title,
                excerpt=excerpt,
                article_id=article_id,
                run_id=run_id,
                model_id=self._cfg.model_id,
                content_hash=content_hash,
            )
            return self._persist(summary)

        # ── LLM call ──────────────────────────────────────────────────────
        prompt = build_article_prompt(
            title=title,
            text=content or excerpt,
            language=language,
            published_at=published_at,
            company_names=company_names,
            market_names=market_names,
            max_input_chars=self._cfg.max_input_chars,
        )

        try:
            raw_payload = self._llm.generate_json(prompt, content_hash=content_hash)
            self._call_count += 1
        except LLMDegradedError as exc:
            logger.warning("article #%d: LLM call failed — %s", article_id, exc)
            summary = _degraded_summary(
                title=title,
                excerpt=excerpt,
                article_id=article_id,
                run_id=run_id,
                model_id=self._cfg.model_id,
                content_hash=content_hash,
            )
            return self._persist(summary)

        # ── schema validation ──────────────────────────────────────────────
        ok, validated, error = validate_article_json(raw_payload)
        if not ok or validated is None:
            logger.error(
                "article #%d: JSON schema validation failed — %s", article_id, error
            )
            summary = _degraded_summary(
                title=title,
                excerpt=excerpt,
                article_id=article_id,
                run_id=run_id,
                model_id=self._cfg.model_id,
                content_hash=content_hash,
            )
            summary.validation_status = "invalid"
            return self._persist(summary)

        validated.article_id = article_id
        validated.run_id = run_id
        validated.model_id = self._cfg.model_id
        validated.prompt_version = self._cfg.prompt_version
        validated.content_hash = content_hash

        logger.debug(
            "article #%d: LLM summary ok — cat=%s importance=%s pertinence=%.2f",
            article_id,
            validated.categorie,
            validated.importance,
            validated.pertinence,
        )
        return self._persist(validated)

    def summarise_run(
        self,
        run_id: int,
        *,
        skip_statuses: tuple[str, ...] = ("duplicate", "redundant", "unrelated"),
        resume: bool = False,
    ) -> list[ArticleSummary]:
        """Summarise all eligible articles of a run in ID order.

        Articles whose ``status`` is in ``skip_statuses`` are silently skipped
        (they contain no useful unique information).

        Args:
            run_id:        PK of the ``daily_run`` row.
            skip_statuses: Article statuses to bypass (default: duplicates, etc.).
            resume:        When ``True``, skip articles that already have a
                           ``summary`` row for this run — idempotent on resume so
                           the LLM is never called twice for the same article.

        Returns:
            List of :class:`ArticleSummary` objects (one per processed article).
        """
        placeholders = ",".join("?" for _ in skip_statuses)
        if resume:
            # Exclude articles that already have a summary row for this run.
            rows = self._db.query(
                f"SELECT id FROM article"
                f" WHERE run_id = ? AND status NOT IN ({placeholders})"
                f" AND id NOT IN ("
                f"   SELECT ref_id FROM summary"
                f"   WHERE run_id = ? AND type = 'article' AND ref_id IS NOT NULL"
                f" )"
                f" ORDER BY id",
                (run_id, *skip_statuses, run_id),
            )
        else:
            rows = self._db.query(
                f"SELECT id FROM article WHERE run_id = ? AND status NOT IN ({placeholders})"
                " ORDER BY id",
                (run_id, *skip_statuses),
            )
        summaries: list[ArticleSummary] = []
        for row in rows:
            try:
                s = self.summarise_article(int(row["id"]), run_id)
                summaries.append(s)
            except ValueError as exc:
                logger.error("summarise_run: skipping article — %s", exc)
        return summaries

    # ── persistence ───────────────────────────────────────────────────────

    def _persist(self, summary: ArticleSummary) -> ArticleSummary:
        """Upsert ``summary`` into the ``summary`` table (idempotent).

        Uses ``ON CONFLICT`` on the ``(run_id, type, ref_id)`` unique index
        (backed by ``uq_summary_run_type_ref_coalesce``).  If a row already
        exists it is replaced in full — safe for ``--resume`` re-runs.
        """
        now = utc_now_iso()
        content = summary.to_text_content()
        json_payload = summary.to_json_payload()
        article_ids_json = json.dumps([summary.article_id])

        try:
            cur = self._db.execute(
                "INSERT INTO summary"
                " (run_id, type, ref_id, content, json_payload, model_id,"
                "  prompt_version, llm_used, content_hash, validation_status,"
                "  article_ids, created_at)"
                " VALUES (?, 'article', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (run_id, type, COALESCE(ref_id, 0))"
                " DO UPDATE SET"
                "   content          = excluded.content,"
                "   json_payload     = excluded.json_payload,"
                "   model_id         = excluded.model_id,"
                "   prompt_version   = excluded.prompt_version,"
                "   llm_used         = excluded.llm_used,"
                "   content_hash     = excluded.content_hash,"
                "   validation_status = excluded.validation_status,"
                "   article_ids      = excluded.article_ids",
                (
                    summary.run_id,
                    summary.article_id,
                    content,
                    json_payload,
                    summary.model_id,
                    summary.prompt_version,
                    int(summary.llm_used),
                    summary.content_hash,
                    summary.validation_status,
                    article_ids_json,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            logger.error("summarizer: DB insert failed for article #%d — %s", summary.article_id, exc)
            raise

        # Retrieve the actual row id (INSERT or pre-existing after UPSERT)
        row = self._db.query_one(
            "SELECT id FROM summary WHERE run_id = ? AND type = 'article' AND ref_id = ?",
            (summary.run_id, summary.article_id),
        )
        summary.db_id = int(row["id"]) if row else None
        return summary


# ══════════════════════════════════════════════════════════════════════════════
# Step 5b — Company / market / global digests
# ══════════════════════════════════════════════════════════════════════════════

DIGEST_PROMPT_VERSION = "1.0"

# Maximum number of article summaries fed into a single digest prompt.
# Beyond this the oldest (by published_at) are dropped to keep the prompt short.
_MAX_ARTICLES_PER_DIGEST = 30

# Maximum characters of a single article's content block inside the digest prompt.
_MAX_ARTICLE_BLOCK_CHARS = 600


# ── Digest dataclass ──────────────────────────────────────────────────────────


@dataclass
class Digest:
    """One company, market, or global digest for a daily run.

    Attributes:
        run_id:            FK to ``daily_run.id``.
        digest_type:       'company' | 'market' | 'global'.
        ref_id:            company_id / market_id / None (global).
        ref_name:          Display name of the entity (for the Markdown heading).
        content:           Full Markdown text with ``[aNN]`` citations.
        llm_used:          True → produced by LLM call; False → degraded heuristic.
        validation_status: 'ok' | 'degraded'.
        model_id:          Model identifier.
        prompt_version:    Digest prompt version (``DIGEST_PROMPT_VERSION``).
        article_ids:       List of article ids that fed into this digest.
        db_id:             ``summary.id`` after persistence; None before.
    """

    run_id: int
    digest_type: str          # 'company' | 'market' | 'global'
    ref_id: int | None        # None for global
    ref_name: str
    content: str
    llm_used: bool
    validation_status: str    # 'ok' | 'degraded'
    model_id: str
    prompt_version: str
    article_ids: list[int] = field(default_factory=list)
    db_id: int | None = None


# ── Article input block (what's fed to the digest prompt) ─────────────────────


@dataclass
class _ArticleBlock:
    """Minimal view of an article summary used to build a digest prompt."""

    article_id: int
    short_code: str           # e.g. "a12"
    title: str
    published_at: str | None
    source_name: str | None
    summary_content: str      # text from summary.content (already produced by 5a)
    importance: str           # from article.importance_level


# ── Prompt builders ───────────────────────────────────────────────────────────


def _format_article_block(block: _ArticleBlock) -> str:
    """Format one article into the compact block used in digest prompts."""
    date_str = block.published_at[:10] if block.published_at else "?"
    source_str = block.source_name or "unknown"
    summary_text = block.summary_content[:_MAX_ARTICLE_BLOCK_CHARS]
    return (
        f"[{block.short_code}] {block.title}\n"
        f"  Date: {date_str} | Source: {source_str} | Importance: {block.importance}\n"
        f"  {summary_text}"
    )


def build_company_digest_prompt(
    company_name: str,
    country_code: str | None,
    articles: list[_ArticleBlock],
) -> str:
    """Build the LLM prompt for a per-company daily digest (§8.3).

    Output expected: Markdown with sections:
    ``## Company``, ``### Faits rapportés``, ``### Conséquences potentielles``,
    ``### Points à vérifier``.
    Each assertion must end with one or more ``[aNN]`` citation tags.
    """
    country = f" ({country_code})" if country_code else ""
    articles_text = "\n\n".join(_format_article_block(a) for a in articles)
    return (
        "Tu es un analyste factuel. Produis un digest quotidien UNIQUEMENT à partir"
        " des articles fournis. Ne rien inventer.\n"
        f"COMPAGNIE : {company_name}{country}\n"
        f"ARTICLES DU JOUR ({len(articles)}) :\n"
        "---\n"
        f"{articles_text}\n"
        "---\n"
        "Produis UNIQUEMENT du Markdown structuré ainsi :\n"
        f"## {company_name}{country}\n"
        "### Faits rapportés\n"
        "- <fait> [aNN]\n"
        "### Conséquences potentielles\n"
        "- <conséquence (horizon, confiance)> [aNN]\n"
        "### Points à vérifier\n"
        "- <point> [aNN]\n\n"
        "Règles : chaque assertion doit citer au moins un article via [aNN] ;"
        " si un champ est vide, écrire '- Aucun'."
    )


def build_market_digest_prompt(
    market_name: str,
    market_type: str,
    articles: list[_ArticleBlock],
) -> str:
    """Build the LLM prompt for a per-market daily digest (§8.3)."""
    articles_text = "\n\n".join(_format_article_block(a) for a in articles)
    return (
        "Tu es un analyste factuel. Produis un digest quotidien UNIQUEMENT à partir"
        " des articles fournis. Ne rien inventer.\n"
        f"MARCHÉ : {market_name} ({market_type})\n"
        f"ARTICLES DU JOUR ({len(articles)}) :\n"
        "---\n"
        f"{articles_text}\n"
        "---\n"
        "Produis UNIQUEMENT du Markdown structuré ainsi :\n"
        f"## {market_name}\n"
        "### Faits\n"
        "- <fait> [aNN]\n"
        "### Points à vérifier\n"
        "- <point> [aNN]\n\n"
        "Règles : chaque assertion doit citer au moins un article via [aNN] ;"
        " si un champ est vide, écrire '- Aucun'."
    )


def build_global_digest_prompt(
    run_date: str,
    company_digests: list[str],
    market_digests: list[str],
) -> str:
    """Build the LLM prompt for the global daily digest (§8.3).

    The global digest synthesises already-produced company and market digests
    (no raw article content), and adds a "Informations critiques" section.
    """
    company_text = "\n\n".join(company_digests) if company_digests else "Aucun digest compagnie."
    market_text = "\n\n".join(market_digests) if market_digests else "Aucun digest marché."
    return (
        "Tu es un analyste factuel. Produis un résumé global du jour"
        " UNIQUEMENT à partir des digests fournis. Ne rien inventer.\n"
        f"DATE : {run_date}\n\n"
        "=== DIGESTS COMPAGNIES ===\n"
        f"{company_text}\n\n"
        "=== DIGESTS MARCHÉS ===\n"
        f"{market_text}\n\n"
        "Produis UNIQUEMENT du Markdown structuré ainsi :\n"
        "## Résumé global\n"
        "(3 à 5 phrases de synthèse)\n\n"
        "### Points critiques\n"
        "- <point critique>\n\n"
        "### Points à vérifier\n"
        "- <point>\n\n"
        "Règles : cite les compagnies et marchés concernés ; ne pas inventer de faits."
    )


# ── Degraded fallback builders ────────────────────────────────────────────────


def _degraded_company_digest(
    *,
    run_id: int,
    company_id: int,
    company_name: str,
    country_code: str | None,
    articles: list[_ArticleBlock],
    model_id: str,
) -> Digest:
    """Heuristic company digest when the LLM is unavailable."""
    country = f" ({country_code})" if country_code else ""
    lines = [f"## {company_name}{country}", "", "### Faits rapportés"]
    for a in articles:
        first_line = (a.summary_content.splitlines() or [""])[0][:200]
        lines.append(f"- {first_line} [{a.short_code}]")
    lines += ["", "### Conséquences potentielles", "- Aucune (mode dégradé)"]
    lines += ["", "### Points à vérifier", "- Vérification humaine recommandée."]
    content = "\n".join(lines)
    return Digest(
        run_id=run_id,
        digest_type="company",
        ref_id=company_id,
        ref_name=company_name,
        content=content,
        llm_used=False,
        validation_status="degraded",
        model_id=model_id,
        prompt_version=DIGEST_PROMPT_VERSION,
        article_ids=[a.article_id for a in articles],
    )


def _degraded_market_digest(
    *,
    run_id: int,
    market_id: int,
    market_name: str,
    market_type: str,
    articles: list[_ArticleBlock],
    model_id: str,
) -> Digest:
    """Heuristic market digest when the LLM is unavailable."""
    lines = [f"## {market_name} ({market_type})", "", "### Faits"]
    for a in articles:
        first_line = (a.summary_content.splitlines() or [""])[0][:200]
        lines.append(f"- {first_line} [{a.short_code}]")
    lines += ["", "### Points à vérifier", "- Vérification humaine recommandée."]
    content = "\n".join(lines)
    return Digest(
        run_id=run_id,
        digest_type="market",
        ref_id=market_id,
        ref_name=market_name,
        content=content,
        llm_used=False,
        validation_status="degraded",
        model_id=model_id,
        prompt_version=DIGEST_PROMPT_VERSION,
        article_ids=[a.article_id for a in articles],
    )


def _degraded_global_digest(
    *,
    run_id: int,
    run_date: str,
    company_digests: list[Digest],
    market_digests: list[Digest],
    model_id: str,
) -> Digest:
    """Heuristic global digest when the LLM is unavailable."""
    company_names = ", ".join(d.ref_name for d in company_digests) or "aucune"
    market_names = ", ".join(d.ref_name for d in market_digests) or "aucun"
    content = (
        f"## Résumé global — {run_date}\n\n"
        f"Compagnies couvertes : {company_names}.\n"
        f"Marchés couverts : {market_names}.\n\n"
        "### Points critiques\n"
        "- Vérification humaine recommandée (mode dégradé).\n\n"
        "### Points à vérifier\n"
        "- Consulter les digests individuels ci-dessous."
    )
    all_article_ids: list[int] = []
    for d in company_digests + market_digests:
        all_article_ids.extend(d.article_ids)
    return Digest(
        run_id=run_id,
        digest_type="global",
        ref_id=None,
        ref_name="global",
        content=content,
        llm_used=False,
        validation_status="degraded",
        model_id=model_id,
        prompt_version=DIGEST_PROMPT_VERSION,
        article_ids=sorted(set(all_article_ids)),
    )


# ── DigestSummarizer ──────────────────────────────────────────────────────────


class DigestSummarizer:
    """Produces company, market, and global digests for a daily run (Step 5b).

    Workflow (must be called after :class:`ArticleSummarizer` has persisted
    all article summaries for the run):

    1. :meth:`digest_company` — for each tracked company that has articles in
       the run, produce one Markdown digest.
    2. :meth:`digest_market`  — same for each tracked market.
    3. :meth:`digest_global`  — synthesise from all company + market digests.
    4. :meth:`digest_run`     — convenience: runs steps 1-3 in order.

    Digest LLM calls are counted in the **same** per-run counter as article
    summaries (passed in via ``call_counter``).  When the cap is reached,
    all remaining digests fall back to degraded mode.

    Usage::

        digester = DigestSummarizer(db=db, llm=llm_client, config=cfg)
        company_digests, market_digests, global_digest = digester.digest_run(run_id)
    """

    def __init__(
        self,
        db: DatabaseManager,
        llm: LLMClientProtocol,
        config: SummarizerConfig,
        *,
        call_counter: list[int] | None = None,
    ) -> None:
        """
        Args:
            db:            Shared database manager.
            llm:           LLM client (real or stub).
            config:        Shared :class:`SummarizerConfig`.
            call_counter:  A single-element list ``[n]`` shared with
                           :class:`ArticleSummarizer` so both classes draw from
                           the same ``max_calls_per_run`` budget.  When ``None``
                           a private counter is used (useful in isolation tests).
        """
        self._db = db
        self._llm = llm
        self._cfg = config
        # Shared mutable counter — list so it is passed by reference.
        self._counter: list[int] = call_counter if call_counter is not None else [0]

    # ── public API ────────────────────────────────────────────────────────

    def digest_run(
        self, run_id: int, *, run_date: str | None = None
    ) -> tuple[list[Digest], list[Digest], Digest]:
        """Produce all digests for *run_id* and return them.

        Args:
            run_id:   PK of the ``daily_run`` row.
            run_date: ISO date string (e.g. '2026-09-08') used in the global
                      digest heading.  If None, queried from the database.

        Returns:
            ``(company_digests, market_digests, global_digest)``
        """
        if run_date is None:
            row = self._db.query_one("SELECT run_date FROM daily_run WHERE id = ?", (run_id,))
            run_date = row["run_date"] if row else "unknown"

        company_digests = self._digest_all_companies(run_id)
        market_digests = self._digest_all_markets(run_id)
        global_digest = self.digest_global(run_id, run_date=run_date,
                                            company_digests=company_digests,
                                            market_digests=market_digests)
        return company_digests, market_digests, global_digest

    def digest_company(
        self,
        run_id: int,
        company_id: int,
        company_name: str,
        *,
        country_code: str | None = None,
    ) -> Digest:
        """Produce and persist a digest for one company.

        Loads the article summaries for articles linked to ``company_id`` in
        this run, then calls the LLM (or falls back to degraded mode).

        Args:
            run_id:       PK of the daily_run.
            company_id:   PK of the company.
            company_name: Display name for the Markdown heading.
            country_code: ISO-2 country code appended to the heading.

        Returns:
            Persisted :class:`Digest` with ``db_id`` filled.
        """
        articles = self._load_article_blocks_for_company(run_id, company_id)
        if not articles:
            # Nothing to summarise — store a placeholder digest.
            digest = Digest(
                run_id=run_id,
                digest_type="company",
                ref_id=company_id,
                ref_name=company_name,
                content=f"## {company_name}\n\nAucun article pour ce run.",
                llm_used=False,
                validation_status="degraded",
                model_id=self._cfg.model_id,
                prompt_version=DIGEST_PROMPT_VERSION,
                article_ids=[],
            )
            return self._persist_digest(digest)

        degraded_reason = self._check_degraded()
        if degraded_reason:
            logger.debug("company digest #%d: degraded — %s", company_id, degraded_reason)
            digest = _degraded_company_digest(
                run_id=run_id,
                company_id=company_id,
                company_name=company_name,
                country_code=country_code,
                articles=articles,
                model_id=self._cfg.model_id,
            )
            return self._persist_digest(digest)

        prompt = build_company_digest_prompt(company_name, country_code, articles)
        try:
            content = self._llm_generate_text(prompt)
        except LLMDegradedError as exc:
            logger.warning("company digest #%d: LLM failed — %s", company_id, exc)
            digest = _degraded_company_digest(
                run_id=run_id,
                company_id=company_id,
                company_name=company_name,
                country_code=country_code,
                articles=articles,
                model_id=self._cfg.model_id,
            )
            return self._persist_digest(digest)

        digest = Digest(
            run_id=run_id,
            digest_type="company",
            ref_id=company_id,
            ref_name=company_name,
            content=content,
            llm_used=True,
            validation_status="ok",
            model_id=self._cfg.model_id,
            prompt_version=DIGEST_PROMPT_VERSION,
            article_ids=[a.article_id for a in articles],
        )
        return self._persist_digest(digest)

    def digest_market(
        self,
        run_id: int,
        market_id: int,
        market_name: str,
        *,
        market_type: str = "sector",
    ) -> Digest:
        """Produce and persist a digest for one market/sector.

        Args:
            run_id:      PK of the daily_run.
            market_id:   PK of the market.
            market_name: Display name for the Markdown heading.
            market_type: 'market' | 'sector' | 'industry'.

        Returns:
            Persisted :class:`Digest` with ``db_id`` filled.
        """
        articles = self._load_article_blocks_for_market(run_id, market_id)
        if not articles:
            digest = Digest(
                run_id=run_id,
                digest_type="market",
                ref_id=market_id,
                ref_name=market_name,
                content=f"## {market_name}\n\nAucun article pour ce run.",
                llm_used=False,
                validation_status="degraded",
                model_id=self._cfg.model_id,
                prompt_version=DIGEST_PROMPT_VERSION,
                article_ids=[],
            )
            return self._persist_digest(digest)

        degraded_reason = self._check_degraded()
        if degraded_reason:
            logger.debug("market digest #%d: degraded — %s", market_id, degraded_reason)
            digest = _degraded_market_digest(
                run_id=run_id,
                market_id=market_id,
                market_name=market_name,
                market_type=market_type,
                articles=articles,
                model_id=self._cfg.model_id,
            )
            return self._persist_digest(digest)

        prompt = build_market_digest_prompt(market_name, market_type, articles)
        try:
            content = self._llm_generate_text(prompt)
        except LLMDegradedError as exc:
            logger.warning("market digest #%d: LLM failed — %s", market_id, exc)
            digest = _degraded_market_digest(
                run_id=run_id,
                market_id=market_id,
                market_name=market_name,
                market_type=market_type,
                articles=articles,
                model_id=self._cfg.model_id,
            )
            return self._persist_digest(digest)

        digest = Digest(
            run_id=run_id,
            digest_type="market",
            ref_id=market_id,
            ref_name=market_name,
            content=content,
            llm_used=True,
            validation_status="ok",
            model_id=self._cfg.model_id,
            prompt_version=DIGEST_PROMPT_VERSION,
            article_ids=[a.article_id for a in articles],
        )
        return self._persist_digest(digest)

    def digest_global(
        self,
        run_id: int,
        *,
        run_date: str,
        company_digests: list[Digest],
        market_digests: list[Digest],
    ) -> Digest:
        """Produce and persist the global digest from company + market digests.

        Args:
            run_id:          PK of the daily_run.
            run_date:        ISO date string for the heading.
            company_digests: Already-produced company digests for this run.
            market_digests:  Already-produced market digests for this run.

        Returns:
            Persisted :class:`Digest` with ``db_id`` filled.
        """
        all_article_ids: list[int] = []
        for d in company_digests + market_digests:
            all_article_ids.extend(d.article_ids)
        all_article_ids = sorted(set(all_article_ids))

        degraded_reason = self._check_degraded()
        if degraded_reason:
            logger.debug("global digest: degraded — %s", degraded_reason)
            digest = _degraded_global_digest(
                run_id=run_id,
                run_date=run_date,
                company_digests=company_digests,
                market_digests=market_digests,
                model_id=self._cfg.model_id,
            )
            return self._persist_digest(digest)

        prompt = build_global_digest_prompt(
            run_date=run_date,
            company_digests=[d.content for d in company_digests],
            market_digests=[d.content for d in market_digests],
        )
        try:
            content = self._llm_generate_text(prompt)
        except LLMDegradedError as exc:
            logger.warning("global digest: LLM failed — %s", exc)
            digest = _degraded_global_digest(
                run_id=run_id,
                run_date=run_date,
                company_digests=company_digests,
                market_digests=market_digests,
                model_id=self._cfg.model_id,
            )
            return self._persist_digest(digest)

        digest = Digest(
            run_id=run_id,
            digest_type="global",
            ref_id=None,
            ref_name="global",
            content=content,
            llm_used=True,
            validation_status="ok",
            model_id=self._cfg.model_id,
            prompt_version=DIGEST_PROMPT_VERSION,
            article_ids=all_article_ids,
        )
        return self._persist_digest(digest)

    # ── private helpers ───────────────────────────────────────────────────

    def _check_degraded(self) -> str | None:
        """Return a reason string if the LLM must not be called, else None."""
        if not self._cfg.enabled:
            return "LLM disabled"
        if self._llm.is_degraded:
            return "circuit-breaker open"
        if self._counter[0] >= self._cfg.max_calls_per_run:
            return f"per-run cap reached ({self._cfg.max_calls_per_run})"
        return None

    def _llm_generate_text(self, prompt: str) -> str:
        """Call the LLM and return the raw text (digests produce Markdown, not JSON).

        Increments the shared call counter on success.

        Raises:
            LLMDegradedError: propagated from the LLM client.
        """
        # Digests use generate() (plain text / Markdown), not generate_json().
        from .llm_client import LLMDegradedError as _LLMDegradedError  # local to avoid circular
        result: str = self._llm.generate(prompt)  # type: ignore[attr-defined]
        self._counter[0] += 1
        return result

    def _load_article_blocks_for_company(
        self, run_id: int, company_id: int
    ) -> list[_ArticleBlock]:
        """Load article+summary blocks for articles linked to a company in this run."""
        rows = self._db.query(
            "SELECT a.id, a.short_code, a.title, a.published_at, a.importance_level,"
            "       s.name AS source_name,"
            "       sm.content AS summary_content"
            " FROM article a"
            " JOIN article_company ac ON ac.article_id = a.id"
            " LEFT JOIN source s ON s.id = a.source_id"
            " LEFT JOIN summary sm ON sm.type = 'article' AND sm.ref_id = a.id AND sm.run_id = a.run_id"
            " WHERE a.run_id = ? AND ac.company_id = ?"
            "   AND a.status NOT IN ('duplicate', 'redundant', 'unrelated')"
            " ORDER BY a.published_at DESC"
            " LIMIT ?",
            (run_id, company_id, _MAX_ARTICLES_PER_DIGEST),
        )
        return [self._row_to_block(r) for r in rows]

    def _load_article_blocks_for_market(
        self, run_id: int, market_id: int
    ) -> list[_ArticleBlock]:
        """Load article+summary blocks for articles linked to a market in this run."""
        rows = self._db.query(
            "SELECT a.id, a.short_code, a.title, a.published_at, a.importance_level,"
            "       s.name AS source_name,"
            "       sm.content AS summary_content"
            " FROM article a"
            " JOIN article_market am ON am.article_id = a.id"
            " LEFT JOIN source s ON s.id = a.source_id"
            " LEFT JOIN summary sm ON sm.type = 'article' AND sm.ref_id = a.id AND sm.run_id = a.run_id"
            " WHERE a.run_id = ? AND am.market_id = ?"
            "   AND a.status NOT IN ('duplicate', 'redundant', 'unrelated')"
            " ORDER BY a.published_at DESC"
            " LIMIT ?",
            (run_id, market_id, _MAX_ARTICLES_PER_DIGEST),
        )
        return [self._row_to_block(r) for r in rows]

    @staticmethod
    def _row_to_block(row: Any) -> _ArticleBlock:
        short_code = f"a{row['id']}"
        return _ArticleBlock(
            article_id=int(row["id"]),
            short_code=short_code,
            title=row["title"] or "",
            published_at=row["published_at"],
            source_name=row["source_name"],
            summary_content=row["summary_content"] or row["title"] or "",
            importance=row["importance_level"] or "moyenne",
        )

    def _digest_all_companies(self, run_id: int) -> list[Digest]:
        """Produce company digests for every company that has articles in this run."""
        rows = self._db.query(
            "SELECT DISTINCT c.id, c.name, c.country_code"
            " FROM company c"
            " JOIN article_company ac ON ac.company_id = c.id"
            " JOIN article a ON a.id = ac.article_id"
            " WHERE a.run_id = ? AND a.status NOT IN ('duplicate','redundant','unrelated')"
            " ORDER BY c.name",
            (run_id,),
        )
        digests: list[Digest] = []
        for row in rows:
            d = self.digest_company(
                run_id,
                int(row["id"]),
                row["name"],
                country_code=row["country_code"],
            )
            digests.append(d)
        return digests

    def _digest_all_markets(self, run_id: int) -> list[Digest]:
        """Produce market digests for every market that has articles in this run."""
        rows = self._db.query(
            "SELECT DISTINCT m.id, m.name, m.type"
            " FROM market m"
            " JOIN article_market am ON am.market_id = m.id"
            " JOIN article a ON a.id = am.article_id"
            " WHERE a.run_id = ? AND a.status NOT IN ('duplicate','redundant','unrelated')"
            " ORDER BY m.name",
            (run_id,),
        )
        digests: list[Digest] = []
        for row in rows:
            d = self.digest_market(
                run_id,
                int(row["id"]),
                row["name"],
                market_type=row["type"] or "sector",
            )
            digests.append(d)
        return digests

    def _persist_digest(self, digest: Digest) -> Digest:
        """Upsert a digest into the ``summary`` table (idempotent).

        Uses the same ``ON CONFLICT`` clause as :class:`ArticleSummarizer`
        (``uq_summary_run_type_ref_coalesce`` index).
        ref_id is NULL for global digests — COALESCE maps that to 0 in the index.
        """
        now = utc_now_iso()
        article_ids_json = json.dumps(digest.article_ids)

        self._db.execute(
            "INSERT INTO summary"
            " (run_id, type, ref_id, content, json_payload, model_id,"
            "  prompt_version, llm_used, content_hash, validation_status,"
            "  article_ids, created_at)"
            " VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?)"
            " ON CONFLICT (run_id, type, COALESCE(ref_id, 0))"
            " DO UPDATE SET"
            "   content           = excluded.content,"
            "   model_id          = excluded.model_id,"
            "   prompt_version    = excluded.prompt_version,"
            "   llm_used          = excluded.llm_used,"
            "   validation_status = excluded.validation_status,"
            "   article_ids       = excluded.article_ids",
            (
                digest.run_id,
                digest.digest_type,
                digest.ref_id,
                digest.content,
                digest.model_id,
                digest.prompt_version,
                int(digest.llm_used),
                digest.validation_status,
                article_ids_json,
                now,
            ),
        )

        # Retrieve the actual row id
        if digest.ref_id is not None:
            row = self._db.query_one(
                "SELECT id FROM summary WHERE run_id=? AND type=? AND ref_id=?",
                (digest.run_id, digest.digest_type, digest.ref_id),
            )
        else:
            row = self._db.query_one(
                "SELECT id FROM summary WHERE run_id=? AND type='global' AND ref_id IS NULL",
                (digest.run_id,),
            )
        digest.db_id = int(row["id"]) if row else None
        return digest
