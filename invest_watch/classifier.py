"""Article classification and scoring module.

Implements specifications §3.4 (F9, F10), §5.2, §8.3 and architecture §4 / §8 (Step 3).
Features:
- Deterministic company matching (name, aliases, ticker, ISIN, domain) in title and content.
- Deterministic market / sector / industry matching (names, codes, and inherited company markets).
- Thematic category detection (financial_results, regulator, product, macro, operations, etc.).
- Scoring calculation:
  * relevance_score (0.0 to 1.0) based on mention counts, title presence, and content density.
  * importance_level ('critique', 'haute', 'moyenne', 'faible') based on category boost,
    source priority boost, and mention amplitude.
- Database persistence for:
  * article (update relevance_score, importance_level, status if unrelated)
  * article_company (company_id, mention_count, relevance_in_article)
  * article_market (market_id, mention_count, relevance_in_article)
  * article_category (category_id, confidence)
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from .company_repository import Company, CompanyRepository
from .config_manager import ScoringSection
from .db_manager import DatabaseManager, utc_now_iso
from .market_repository import Market, MarketRepository

# Canonical taxonomy of categories (from specs §8.3 / §11)
CANONICAL_CATEGORIES = {
    "financial_results": [
        "resultats", "results", "earnings", "revenue", "ebitda", "profit", "benefice",
        "chiffre d'affaires", "guidance", "q1", "q2", "q3", "q4", "annual report",
        "rapport annuel", "margin", "marge", "dividend", "dividende", "quarterly", "trimestriel",
        "财报", "业绩", "营收", "利润", "净利润"
    ],
    "regulator": [
        "sec", "csrc", "hkex", "regulator", "regulation", "sanction", "compliance",
        "antitrust", "investigation", "enquete", "amende", "fine", "audit", "filing",
        "depot", "disclosure", "monopoly", "autorite", "监管", "证监会", "罚款", "合规"
    ],
    "corporate_actions": [
        "split", "buyback", "rachat d'actions", "dividend", "offering", "placement",
        "restructuring", "restructuration", "spinoff", "ipo", "listing", "capital increase",
        "augmentation de capital", "回购", "拆股", "增发", "上市"
    ],
    "mergers_acquisitions": [
        "acquisition", "merger", "fusion", "takeover", "buyout", "joint venture",
        "jv", "cession", "divestiture", "rachete", "acquires", "acquiert", "并购", "收购", "合资"
    ],
    "product": [
        "launch", "lancement", "new model", "nouveau modele", "unveil", "devoile",
        "patent", "brevet", "innovation", "technology", "technologie", "battery",
        "batterie", "platform", "plateforme", "release", "发布", "新车", "专利", "技术"
    ],
    "operations": [
        "factory", "usine", "production", "gigafactory", "supply chain", "chaine d'approvisionnement",
        "capacity", "capacite", "plant", "shutdown", "arret", "strike", "greve",
        "delivery", "livraison", "shipment", "expedition", "产能", "工厂", "投产", "交付"
    ],
    "macro": [
        "interest rate", "taux d'interet", "inflation", "cpi", "pmi", "gdp", "pib",
        "central bank", "banque centrale", "fed", "pboc", "ecb", "bce", "stimulus",
        "relance", "tariff", "droits de douane", "tax", "subsidies", "subventions",
        "通胀", "降息", "央行", "关税", "补贴"
    ],
    "geopolitical": [
        "sanctions", "trade war", "guerre commerciale", "export controls", "embargo",
        "taiwan", "brics", "treaty", "traite", "diplomacy", "diplomatie", "geopolitics",
        "制裁", "贸易战", "出口管制"
    ],
    "competitive": [
        "market share", "part de marche", "price war", "guerre des prix", "competitor",
        "concurrent", "ranking", "classement", "leader", "challenge", "市占率", "价格战", "竞争"
    ],
    "sustainability": [
        "esg", "carbon", "carbone", "emission", "green", "vert", "renewable", "renouvelable",
        "recycling", "recyclage", "net zero", "co2", "decarbonation", "减碳", "环保", "新能源"
    ],
    "other": [],
}

_WORD_BOUNDARY_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _count_matches(pattern: str, text: str, is_cjk_or_exact: bool = False) -> int:
    """Case-insensitive pattern occurrence count in text.
    
    If pattern is short Latin (e.g. ticker 'BYD', 'NIO') or standard words, use word boundaries.
    For CJK patterns, direct substring count.
    """
    if not pattern or not text:
        return 0
    # CJK character range check
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in pattern)
    if has_cjk:
        return text.count(pattern)
    escaped = re.escape(pattern)
    regex = re.compile(rf"\b{escaped}\b", re.IGNORECASE)
    return len(regex.findall(text))


@dataclass
class CompanyMatch:
    company: Company
    mention_count: int
    title_mentions: int
    content_mentions: int
    relevance: float  # 0.0 to 1.0 within the article


@dataclass
class MarketMatch:
    market: Market
    mention_count: int
    relevance: float  # 0.0 to 1.0


@dataclass
class CategoryMatch:
    name: str
    confidence: float  # 0.0 to 1.0


@dataclass
class ClassificationResult:
    article_id: int
    companies: list[CompanyMatch] = field(default_factory=list)
    markets: list[MarketMatch] = field(default_factory=list)
    categories: list[CategoryMatch] = field(default_factory=list)
    relevance_score: float = 0.0
    importance_level: str = "moyenne"  # 'critique', 'haute', 'moyenne', 'faible'


class Classifier:
    """Rules-based classifier and scoring engine."""

    def __init__(
        self,
        db: DatabaseManager,
        scoring_config: ScoringSection | None = None,
    ) -> None:
        self.db = db
        self.scoring = scoring_config or ScoringSection(
            relevance_min=0.5,
            importance_default="moyenne",
            importance_boost_categories=("regulator", "financial_results"),
            importance_boost_source_priority=(1, 2),
        )
        self.company_repo = CompanyRepository(db)
        self.market_repo = MarketRepository(db)
        self._ensure_categories_in_db()

    def _ensure_categories_in_db(self) -> None:
        """Seed canonical categories table if empty."""
        for name, keywords in CANONICAL_CATEGORIES.items():
            self.db.execute(
                "INSERT INTO category (name, description) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
                (name, f"Category for {name} news"),
            )

    def _get_category_id_map(self) -> dict[str, int]:
        rows = self.db.query("SELECT id, name FROM category")
        return {r["name"]: int(r["id"]) for r in rows}

    def classify_text(
        self,
        title: str,
        content: str | None = None,
        source_priority: int = 3,
        source_type: str = "search",
    ) -> tuple[list[CompanyMatch], list[MarketMatch], list[CategoryMatch], float, str]:
        """Perform deterministic classification and score computation without DB writes."""
        title_text = title or ""
        body_text = content or ""
        full_text = f"{title_text}\n{body_text}".lower()

        # 1. Match companies
        active_companies = self.company_repo.list()
        company_matches: list[CompanyMatch] = []

        for comp in active_companies:
            aliases = self.company_repo.aliases_for(comp.id) if comp.id else []
            patterns: set[str] = {comp.name}
            if comp.ticker:
                patterns.add(comp.ticker)
            for a in aliases:
                patterns.add(a.term)

            t_mentions = sum(_count_matches(p, title_text) for p in patterns)
            c_mentions = sum(_count_matches(p, body_text) for p in patterns)
            total_mentions = t_mentions + c_mentions

            if total_mentions > 0:
                # Calculate intra-article relevance (title match weighs heavily)
                rel = min(1.0, 0.4 * min(t_mentions, 2) + 0.1 * min(c_mentions, 6))
                company_matches.append(
                    CompanyMatch(
                        company=comp,
                        mention_count=total_mentions,
                        title_mentions=t_mentions,
                        content_mentions=c_mentions,
                        relevance=rel,
                    )
                )

        # Sort companies by relevance desc
        company_matches.sort(key=lambda m: (m.title_mentions > 0, m.mention_count), reverse=True)

        # 2. Match markets
        active_markets = self.market_repo.list()
        market_matches: dict[int, MarketMatch] = {}

        for mkt in active_markets:
            m_patterns = [mkt.name]
            if mkt.code:
                m_patterns.append(mkt.code)
            mentions = sum(_count_matches(p, full_text) for p in m_patterns)
            if mentions > 0:
                rel = min(1.0, 0.2 + 0.1 * min(mentions, 8))
                market_matches[mkt.id] = MarketMatch(market=mkt, mention_count=mentions, relevance=rel)

        # Inherit markets from matched companies
        for cm in company_matches:
            if cm.company.id:
                for comp_mkt, weight in self.company_repo.markets_for(cm.company.id):
                    if comp_mkt.id not in market_matches:
                        market_matches[comp_mkt.id] = MarketMatch(
                            market=comp_mkt,
                            mention_count=cm.mention_count,
                            relevance=min(1.0, round(cm.relevance * weight, 2)),
                        )

        market_list = list(market_matches.values())
        market_list.sort(key=lambda m: m.relevance, reverse=True)

        # 3. Match categories
        category_matches: list[CategoryMatch] = []
        for cat_name, keywords in CANONICAL_CATEGORIES.items():
            if not keywords:
                continue
            cat_hits = sum(_count_matches(kw, full_text) for kw in keywords)
            if cat_hits > 0:
                conf = min(1.0, 0.3 + 0.15 * min(cat_hits, 5))
                category_matches.append(CategoryMatch(name=cat_name, confidence=conf))

        if not category_matches:
            category_matches.append(CategoryMatch(name="other", confidence=0.5))
        else:
            category_matches.sort(key=lambda c: c.confidence, reverse=True)

        # 4. Compute overall relevance_score (0.0 to 1.0)
        relevance_score = 0.0
        if company_matches:
            top_c = company_matches[0]
            # Base from top company relevance + title presence
            base = 0.5 if top_c.title_mentions > 0 else 0.3
            relevance_score = min(1.0, base + 0.1 * min(top_c.mention_count, 5))
        elif market_list:
            top_m = market_list[0]
            relevance_score = min(0.7, 0.2 + 0.1 * min(top_m.mention_count, 5))
        else:
            relevance_score = 0.1

        # 5. Compute importance_level ('critique', 'haute', 'moyenne', 'faible')
        primary_cat = category_matches[0].name if category_matches else "other"
        importance = self.scoring.importance_default  # "moyenne"

        is_boost_cat = primary_cat in self.scoring.importance_boost_categories
        is_boost_source = source_priority in self.scoring.importance_boost_source_priority

        if is_boost_cat and is_boost_source and relevance_score >= 0.7:
            importance = "critique"
        elif is_boost_cat or (is_boost_source and relevance_score >= 0.5):
            importance = "haute"
        elif relevance_score < self.scoring.relevance_min:
            importance = "faible"

        return (
            company_matches,
            market_list,
            category_matches,
            round(relevance_score, 2),
            importance,
        )

    def classify_article(self, article_id: int) -> ClassificationResult:
        """Classify an existing article from the database, save junctions, and update scores."""
        row = self.db.query_one(
            "SELECT a.*, s.priority as source_priority, s.type as source_type"
            " FROM article a LEFT JOIN source s ON a.source_id = s.id"
            " WHERE a.id = ?",
            (article_id,),
        )
        if row is None:
            raise ValueError(f"article #{article_id} not found")

        source_prio = int(row["source_priority"]) if row["source_priority"] is not None else 3
        source_type = str(row["source_type"]) if row["source_type"] is not None else "search"

        comp_matches, mkt_matches, cat_matches, rel_score, importance = self.classify_text(
            title=row["title"],
            content=row["content"] or row["excerpt"],
            source_priority=source_prio,
            source_type=source_type,
        )

        cat_id_map = self._get_category_id_map()
        now = utc_now_iso()

        # Update article fields & junctions in a transaction
        with self.db.transaction():
            # If no company or market matches and relevance is near 0, flag as unrelated status
            new_status = row["status"]
            if not comp_matches and not mkt_matches and rel_score < 0.2:
                new_status = "unrelated"

            self.db.execute(
                "UPDATE article SET relevance_score = ?, importance_level = ?, status = ?, updated_at = ?"
                " WHERE id = ?",
                (rel_score, importance, new_status, now, article_id),
            )

            # Clear existing junctions for idempotent reclassification
            self.db.execute("DELETE FROM article_company WHERE article_id = ?", (article_id,))
            self.db.execute("DELETE FROM article_market WHERE article_id = ?", (article_id,))
            self.db.execute("DELETE FROM article_category WHERE article_id = ?", (article_id,))

            # Insert article_company
            for cm in comp_matches:
                self.db.execute(
                    "INSERT INTO article_company (article_id, company_id, mention_count, relevance_in_article)"
                    " VALUES (?, ?, ?, ?)",
                    (article_id, cm.company.id, cm.mention_count, cm.relevance),
                )

            # Insert article_market
            for mm in mkt_matches:
                self.db.execute(
                    "INSERT INTO article_market (article_id, market_id, mention_count, relevance_in_article)"
                    " VALUES (?, ?, ?, ?)",
                    (article_id, mm.market.id, mm.mention_count, mm.relevance),
                )

            # Insert article_category
            for cat in cat_matches:
                cid = cat_id_map.get(cat.name)
                if cid is not None:
                    self.db.execute(
                        "INSERT INTO article_category (article_id, category_id, confidence)"
                        " VALUES (?, ?, ?)",
                        (article_id, cid, cat.confidence),
                    )

        return ClassificationResult(
            article_id=article_id,
            companies=comp_matches,
            markets=mkt_matches,
            categories=cat_matches,
            relevance_score=rel_score,
            importance_level=importance,
        )

    def classify_run(self, run_id: int) -> list[ClassificationResult]:
        """Classify all articles associated with a given daily_run."""
        rows = self.db.query("SELECT id FROM article WHERE run_id = ?", (run_id,))
        results: list[ClassificationResult] = []
        for r in rows:
            results.append(self.classify_article(int(r["id"])))
        return results
