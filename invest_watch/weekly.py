"""Weekly per-company summaries (``invest-watch weekly``).

Daily runs can work without the LLM ( ``[llm] enabled = false`` ); this module
spends the limited watsonx budget once a week instead: ONE call per company,
fed with the most important articles of the ISO week (Monday-Sunday, local
time), French translation preferred when available.

- ``[weekly] use_llm`` is independent of ``[llm] enabled`` (same credentials,
  model and endpoint as ``[llm]``).
- Results are stored in ``weekly_summary`` (one row per week + company); a
  company already summarised by the LLM for that week is skipped unless
  ``force=True`` - an interrupted run (quota) only re-pays what is missing.
- When the LLM is unavailable a degraded summary (titles + short codes) is
  written so the report is always complete.
- Report: ``reports/weekly/YYYY-Www.md`` + ``.json``.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db_manager import DatabaseManager, utc_now_iso
from .llm_client import LLMDegradedError
from .report_generator import google_translate_url
from .weekly_config import WeeklySection

logger = logging.getLogger(__name__)

PROMPT_VERSION = "weekly-v3"
LINK_GROUPS = ("direct", "sector", "peer")
GROUP_LABELS = {"direct": "Soci\u00e9t\u00e9", "sector": "Secteur", "peer": "Concurrents / partenaires"}
_PROMPT_GROUP = {"direct": "SOCIETE", "sector": "SECTEUR", "peer": "CONCURRENT"}
_IMPORTANCE_RANK = {"critique": 0, "haute": 1, "moyenne": 2, "faible": 3}
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_EXCLUDED_STATUS = ("duplicate", "redundant", "unrelated")


@dataclass(frozen=True)
class IsoWeek:
    label: str          # 2026-W41
    start: _dt.date     # Monday
    end: _dt.date       # Sunday


def iso_week(value: str | None = None, *, today: _dt.date | None = None) -> IsoWeek:
    """Parse ``YYYY-Www``; ``None`` means the last complete week before ``today``."""
    if value:
        m = _WEEK_RE.match(value.strip().upper())
        if not m:
            raise ValueError(f"invalid ISO week {value!r} (expected e.g. 2026-W41)")
        start = _dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    else:
        today = today or _dt.date.today()
        start = today - _dt.timedelta(days=today.weekday() + 7)
    year, week, _ = start.isocalendar()
    return IsoWeek(label=f"{year}-W{week:02d}", start=start, end=start + _dt.timedelta(days=6))


@dataclass
class CompanyWeekly:
    company_id: int
    company_name: str
    articles: list[dict] = field(default_factory=list)
    prompt: str = ""
    content: str = ""
    llm_used: bool = False
    skipped: bool = False      # already summarised by the LLM (no --force)
    error: str | None = None


def build_weekly_prompt(company: str, week: IsoWeek, blocks: list[str]) -> str:
    """French prompt for one company's weekly summary."""
    return (
        "Tu es un analyste financier factuel. Tu r\u00e9diges UNIQUEMENT \u00e0 partir des articles fournis.\n"
        "R\u00e9dige TOUJOURS en fran\u00e7ais, m\u00eame si les articles sont en chinois ou en anglais.\n"
        f"Soci\u00e9t\u00e9 suivie : {company}\n"
        f"P\u00e9riode : semaine {week.label} (du {week.start.isoformat()} au {week.end.isoformat()})\n"
        "Produis un r\u00e9sum\u00e9 hebdomadaire en Markdown avec exactement ces sections :\n"
        "### Faits marquants de la semaine\n"
        "### Chiffres cl\u00e9s\n"
        "### Catalyseurs et risques \u00e0 venir\n"
        "### Points \u00e0 v\u00e9rifier\n"
        "R\u00e8gles : puces courtes ; chaque puce cite ses sources avec leur code entre crochets, "
        "par exemple [8f25cccb] ; n'invente aucun chiffre ; ignore les articles sans rapport "
        f"avec {company} ; si une section est vide, \u00e9cris \"- Rien de notable.\"\n"
        "Chaque article porte un groupe : SOCIETE (parle directement de la soci\u00e9t\u00e9), "
        "SECTEUR (march\u00e9 / technologie de la soci\u00e9t\u00e9) ou CONCURRENT (concurrent ou partenaire). "
        f"Les articles SECTEUR et CONCURRENT servent de contexte : n'en retiens que ce qui \u00e9claire "
        f"la situation de {company} (demande, prix, concurrence, r\u00e9glementation) et dis-le explicitement.\n"
        "Cours de bourse : ne donne JAMAIS de cours d'action, de prix cible, ni de variation chiffr\u00e9e "
        "(pourcentage ou montant) du titre ; indique seulement la tendance qualitative "
        "(le titre a plut\u00f4t bien, mal ou peu r\u00e9agi). Les chiffres d'activit\u00e9 "
        "(chiffre d'affaires, r\u00e9sultats, volumes, contrats) restent autoris\u00e9s.\n"
        "--- ARTICLES ---\n"
        + "\n\n".join(blocks)
        + "\n--- FIN ---\n"
    )


def _article_block(a: dict, max_chars: int) -> str:
    text = (a.get("content_fr") or a.get("content") or a.get("excerpt") or "").strip()
    text = re.sub(r"\s+", " ", text)[:max_chars]
    title = a.get("title_fr") or a.get("title") or ""
    date = (a.get("published_at") or a.get("collected_at") or "")[:10]
    group = _PROMPT_GROUP.get(a.get("link_type") or "direct", "SOCIETE")
    return f"[{a['short_code']}] {date} ({group}, {a.get('importance_level') or '-'}) {title}\n{text}"


def _degraded_content(c: CompanyWeekly, reason: str) -> str:
    lines = [f"*(r\u00e9sum\u00e9 d\u00e9grad\u00e9 - {reason})*", "", "### Articles de la semaine"]
    if not c.articles:
        lines.append("- Aucun article retenu cette semaine.")
    for a in c.articles:
        title = a.get("title_fr") or a.get("title") or ""
        lines.append(f"- **{title}** [{a['short_code']}]")
    return "\n".join(lines)


def _rank_key(a: dict) -> tuple:
    return (
        -float(a.get("relevance_in_article") or 0),
        _IMPORTANCE_RANK.get(a.get("importance_level") or "", 9),
        -float(a.get("relevance_score") or 0),
    )


def select_with_quotas(items: list[dict], quotas: dict[str, int], cap: int) -> list[dict]:
    """Pick at most ``quotas[group]`` articles per link group, best first.

    Within a group: relevance for the company, then importance. Slots a group
    leaves unused go to the best remaining articles of the other groups, up
    to ``cap`` in total. Result is ordered direct, sector, peer.
    """
    groups: dict[str, list[dict]] = {g: [] for g in LINK_GROUPS}
    for a in items:
        groups.get(a.get("link_type") or "direct", groups["direct"]).append(a)
    chosen: dict[str, list[dict]] = {}
    rest: list[dict] = []
    for g in LINK_GROUPS:
        ranked = sorted(groups[g], key=_rank_key)
        n = max(0, int(quotas.get(g, 0)))
        chosen[g], leftover = ranked[:n], ranked[n:]
        rest.extend(leftover)
    total = sum(len(v) for v in chosen.values())
    if total > cap:  # quotas larger than the cap: trim the thematic groups first
        for g in reversed(LINK_GROUPS):
            while total > cap and chosen[g]:
                chosen[g].pop()
                total -= 1
    for a in sorted(rest, key=_rank_key)[: max(0, cap - total)]:
        chosen[a.get("link_type") or "direct"].append(a)
    return [a for g in LINK_GROUPS for a in sorted(chosen[g], key=_rank_key)]


def group_counts(articles: list[dict]) -> dict[str, int]:
    counts = {g: 0 for g in LINK_GROUPS}
    for a in articles:
        g = a.get("link_type") or "direct"
        counts[g if g in counts else "direct"] += 1
    return counts


class WeeklySummarizer:
    """Builds, persists and reports the weekly per-company summaries."""

    def __init__(
        self,
        db: DatabaseManager,
        weekly_cfg: WeeklySection,
        *,
        llm_client: Any | None = None,
        model_id: str | None = None,
        reports_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.cfg = weekly_cfg
        self.llm = llm_client
        self.model_id = model_id
        self.reports_dir = reports_dir

    # -- selection ----------------------------------------------------------------

    def companies(self, name: str | None = None) -> list[tuple[int, str]]:
        rows = self.db.query("SELECT id, name FROM company WHERE active = 1 ORDER BY name COLLATE NOCASE")
        out = [(int(r["id"]), str(r["name"])) for r in rows]
        if name:
            out = [c for c in out if c[1].casefold() == name.casefold()]
            if not out:
                raise ValueError(f"unknown company {name!r}")
        return out

    def articles_for(self, company_id: int, week: IsoWeek) -> list[dict]:
        placeholders = ",".join("?" for _ in _EXCLUDED_STATUS)
        rows = self.db.query(
            "SELECT a.id, a.short_code, a.title, a.title_fr, a.content, a.content_fr, a.excerpt,"
            " a.url_original, a.language, a.published_at, a.collected_at, a.importance_level,"
            " a.relevance_score, ac.relevance_in_article, ac.link_type"
            " FROM article a JOIN article_company ac ON ac.article_id = a.id"
            " WHERE ac.company_id = ?"
            f"   AND a.status NOT IN ({placeholders})"
            "   AND substr(COALESCE(a.published_at, a.collected_at), 1, 10) BETWEEN ? AND ?",
            (company_id, *_EXCLUDED_STATUS, week.start.isoformat(), week.end.isoformat()),
        )
        return select_with_quotas(
            [dict(r) for r in rows],
            {"direct": self.cfg.quota_direct, "sector": self.cfg.quota_sector, "peer": self.cfg.quota_peer},
            self.cfg.max_articles_per_company,
        )

    def _existing(self, week: IsoWeek, company_id: int) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM weekly_summary WHERE week = ? AND company_id = ?", (week.label, company_id)
        )
        return dict(row) if row else None

    # -- main entry -------------------------------------------------------------

    def run(
        self,
        week: IsoWeek,
        *,
        company: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> list[CompanyWeekly]:
        results: list[CompanyWeekly] = []
        for company_id, name in self.companies(company):
            c = CompanyWeekly(company_id=company_id, company_name=name)
            c.articles = self.articles_for(company_id, week)
            blocks: list[str] = []
            # the cap covers the whole prompt: instructions + article blocks + separators
            total = len(build_weekly_prompt(name, week, []))
            for a in c.articles:
                block = _article_block(a, self.cfg.max_chars_per_article)
                if total + len(block) + 2 > self.cfg.max_input_chars:
                    break
                blocks.append(block)
                total += len(block) + 2
            c.articles = c.articles[: len(blocks)]
            c.prompt = build_weekly_prompt(name, week, blocks) if blocks else ""
            existing = self._existing(week, company_id)
            if existing and existing["llm_used"] and not force:
                c.skipped = True
                c.content = existing["content"]
                c.llm_used = True
                results.append(c)
                continue
            if dry_run:
                results.append(c)
                continue
            self._summarise(c)
            self._persist(c, week)
            results.append(c)
        if not dry_run and self.reports_dir is not None:
            self.write_report(week, results)
        return results

    def _summarise(self, c: CompanyWeekly) -> None:
        if not c.articles:
            c.content = "- Aucun article retenu cette semaine."
            return
        if not self.cfg.use_llm or self.llm is None:
            c.content = _degraded_content(c, "LLM d\u00e9sactiv\u00e9 pour l'hebdo")
            return
        try:
            text = (self.llm.generate(c.prompt) or "").strip()
        except LLMDegradedError as exc:
            c.error = str(exc)
            logger.warning("weekly '%s': LLM failed - %s", c.company_name, exc)
            c.content = _degraded_content(c, "LLM indisponible")
            return
        if not text:
            c.error = "empty LLM answer"
            c.content = _degraded_content(c, "r\u00e9ponse LLM vide")
            return
        c.content = text
        c.llm_used = True

    def _persist(self, c: CompanyWeekly, week: IsoWeek) -> None:
        self.db.execute(
            "INSERT INTO weekly_summary (week, company_id, period_start, period_end, content, llm_used,"
            " validation_status, model_id, prompt_version, article_ids, article_count, input_chars, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(week, company_id) DO UPDATE SET content=excluded.content,"
            " llm_used=excluded.llm_used, validation_status=excluded.validation_status,"
            " model_id=excluded.model_id, prompt_version=excluded.prompt_version,"
            " article_ids=excluded.article_ids, article_count=excluded.article_count,"
            " input_chars=excluded.input_chars, created_at=excluded.created_at",
            (
                week.label, c.company_id, week.start.isoformat(), week.end.isoformat(), c.content,
                int(c.llm_used), "ok" if c.llm_used else "degraded",
                self.model_id if c.llm_used else None, PROMPT_VERSION,
                json.dumps([a["id"] for a in c.articles]), len(c.articles), len(c.prompt), utc_now_iso(),
            ),
        )

    # -- report -----------------------------------------------------------------

    def write_report(self, week: IsoWeek, results: list[CompanyWeekly]) -> tuple[Path, Path]:
        out_dir = Path(self.reports_dir) / "weekly"
        out_dir.mkdir(parents=True, exist_ok=True)
        md_lines = [
            f"# R\u00e9sum\u00e9 hebdomadaire - {week.label}",
            "",
            f"*Du {week.start.isoformat()} au {week.end.isoformat()} - "
            f"{sum(1 for r in results if r.llm_used)}/{len(results)} soci\u00e9t\u00e9s r\u00e9sum\u00e9es par le LLM.*",
            "",
        ]
        payload: dict[str, Any] = {
            "week": week.label, "period_start": week.start.isoformat(),
            "period_end": week.end.isoformat(), "companies": [],
        }
        for r in results:
            md_lines.append(f"## {r.company_name}")
            md_lines.append("")
            md_lines.append(r.content or "- Aucun article retenu cette semaine.")
            md_lines.append("")
            if r.articles:
                md_lines.append("**Sources**")
                md_lines.append("")
                for g in LINK_GROUPS:
                    group = [a for a in r.articles if (a.get("link_type") or "direct") == g]
                    if not group:
                        continue
                    md_lines.append(f"*{GROUP_LABELS[g]}*")
                    md_lines.append("")
                    for a in group:
                        url = a.get("url_original") or ""
                        fr = google_translate_url(url, a.get("language"))
                        title = a.get("title_fr") or a.get("title") or ""
                        line = f"- [{a['short_code']}]({url}) {title}" if url else f"- [{a['short_code']}] {title}"
                        if fr:
                            line += f" \u00b7 [FR]({fr})"
                        md_lines.append(line)
                    md_lines.append("")
            payload["companies"].append({
                "company": r.company_name, "llm_used": r.llm_used, "content": r.content,
                "articles": [{"short_code": a["short_code"], "title": a.get("title_fr") or a.get("title"),
                              "link_type": a.get("link_type") or "direct",
                              "url": a.get("url_original"),
                              "translate_url": google_translate_url(a.get("url_original"), a.get("language"))}
                             for a in r.articles],
            })
        md_path = out_dir / f"{week.label}.md"
        json_path = out_dir / f"{week.label}.json"
        _atomic_write(md_path, "\n".join(md_lines))
        _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
        return md_path, json_path


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def build_weekly_llm_client(cfg: Any) -> Any | None:
    """watsonx client for the weekly job: same [llm] settings, gated by [weekly].use_llm only."""
    if not cfg.weekly.use_llm:
        return None
    from .llm_client import WatsonxLLMClient, llm_client_config_from_section

    client_cfg = dataclasses.replace(llm_client_config_from_section(cfg.llm), enabled=True)
    return WatsonxLLMClient(client_cfg)
