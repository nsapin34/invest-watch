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


_AMBIGUOUS_TICKER_MAX_LEN = 3


def _ticker_patterns(ticker: str) -> list[str]:
    """Unambiguous contextual forms for a short ticker (e.g. 'AI')."""
    t = re.escape(ticker)
    return [
        rf"\({t}\)",                                      # "(AI)"
        rf"\b(?:EPA|ENXTPA|PA|Euronext)\s*[:.]\s*{t}\b",  # "EPA:AI"
        rf"\b{t}\.PA\b",                                  # "AI.PA"
    ]


def _count_ticker(ticker: str, text: str) -> int:
    """Count ticker mentions; short tickers only in unambiguous contexts.

    Long tickers: whole word, case-sensitive. Short tickers (<= 3 chars, e.g.
    Air Liquide 'AI') collide with common words, so only contextual forms count.
    """
    if not ticker or not text:
        return 0
    if len(ticker) > _AMBIGUOUS_TICKER_MAX_LEN:
        return len(re.findall(rf"\b{re.escape(ticker)}\b", text))
    return sum(len(re.findall(p, text)) for p in _ticker_patterns(ticker))


LINK_TYPES = ("direct", "sector", "peer")
_TOPIC_WEIGHT = 0.15  # topic score per distinct keyword found


_CASE_SENSITIVE_MAX_LEN = 4


def _count_term(term: str, text: str) -> int:
    """Count a topic keyword / peer name, guarding against common-word collisions.

    CJK: substring. Short (<= 4 chars) or all-caps latin terms ('SiC', 'BP',
    'EV', 'LNG') match whole-word AND case-sensitively, so 'sic', 'bp' or
    'ev' in running text do not count. Longer terms: case-insensitive.
    Ambiguous longer names (Shell, Ford) belong in config in a
    disambiguated form ('Shell plc', 'Ford Motor').
    """
    if not term or not text:
        return 0
    if any("\u4e00" <= ch <= "\u9fff" for ch in term):
        return text.count(term)
    if len(term) <= _CASE_SENSITIVE_MAX_LEN or (term.isupper() and len(term) > 1):
        return len(re.findall(rf"(?<!\w){re.escape(term)}(?!\w)", text))
    return _count_matches(term, text)


def topic_score(keywords: Sequence[str], text: str) -> float:
    """Share of the company's theme covered by ``text`` (0..1).

    Counts DISTINCT ``topic_keywords`` present (case-insensitive, word boundary
    for latin terms, substring for CJK), 0.15 each, capped at 1.
    """
    if not keywords or not text:
        return 0.0
    return round(min(1.0, _TOPIC_WEIGHT * topic_hits(keywords, text)), 2)


def topic_hits(keywords: Sequence[str], text: str) -> int:
    """Number of DISTINCT topic keywords present in ``text``."""
    if not keywords or not text:
        return 0
    return sum(1 for kw in keywords if _count_term(kw, text) > 0)


@dataclass
class CompanyMatch:
    company: Company
    mention_count: int
    title_mentions: int
    content_mentions: int
    relevance: float  # 0.0 to 1.0 within the article
    link_type: str = "direct"  # v010: 'direct' | 'sector' | 'peer'
    topic_score: float = 0.0


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
        raw_text = f"{title_text}\n{body_text}"
        full_text = raw_text.lower()

        # 1. Match companies
        active_companies = self.company_repo.list()
        company_matches: list[CompanyMatch] = []

        for comp in active_companies:
            aliases = self.company_repo.aliases_for(comp.id) if comp.id else []
            patterns: set[str] = {comp.name}
            for a in aliases:
                patterns.add(a.term)

            t_mentions = sum(_count_matches(p, title_text) for p in patterns)
            c_mentions = sum(_count_matches(p, body_text) for p in patterns)
            if comp.ticker and comp.ticker not in patterns:
                t_tick = _count_ticker(comp.ticker, title_text)
                c_tick = _count_ticker(comp.ticker, body_text)
                # A short ticker never matches on its own: it only reinforces
                # a name/alias already found (e.g. 'AI' vs "AI regulation").
                if len(comp.ticker) > _AMBIGUOUS_TICKER_MAX_LEN or (t_mentions + c_mentions) > 0:
                    t_mentions += t_tick
                    c_mentions += c_tick
            total_mentions = t_mentions + c_mentions
            kws = getattr(comp, "topic_keywords", ()) or ()
            hits = topic_hits(kws, raw_text)
            topic = round(min(1.0, _TOPIC_WEIGHT * hits), 2)

            if total_mentions > 0:
                # direct: one mention is enough; then the THEME decides, not the count.
                rel = 0.4 + (0.2 if t_mentions > 0 else 0.0) + 0.4 * topic
                link = "direct"
            else:
                peer_hits = 0
                if hits >= max(1, self.scoring.peer_min_topic_hits):
                    peer_hits = sum(_count_term(p, raw_text) for p in (getattr(comp, "peers", ()) or ()))
                if peer_hits > 0:
                    # peer: a competitor/partner named in an on-topic article
                    rel, link, total_mentions = 0.1 + 0.4 * topic, "peer", peer_hits
                elif topic >= self.scoring.topic_link_min:
                    rel, link = 0.4 * topic, "sector"
                else:
                    continue
            company_matches.append(
                CompanyMatch(
                    company=comp,
                    mention_count=total_mentions,
                    title_mentions=t_mentions,
                    content_mentions=c_mentions,
                    relevance=round(min(1.0, rel), 2),
                    link_type=link,
                    topic_score=topic,
                )
            )

        # Thematic links yield to direct links on OTHER tracked companies:
        # - sector: dropped as soon as another tracked company is named (the
        #   article is about that company, e.g. an Ecopetrol rating is not
        #   TotalEnergies sector news);
        # - peer: kept only when every other directly named tracked company is
        #   one of this company's peers (a BYD article stays a CATL peer link).
        direct_names = {m.company.name.casefold() for m in company_matches if m.link_type == "direct"}

        def _keep(m: CompanyMatch) -> bool:
            if m.link_type == "direct":
                return True
            others = direct_names - {m.company.name.casefold()}
            if not others:
                return True
            if m.link_type == "peer":
                return others <= {p.casefold() for p in (getattr(m.company, "peers", ()) or ())}
            return False

        company_matches = [m for m in company_matches if _keep(m)]

        # Direct links first (title presence, mentions), then thematic links by relevance
        company_matches.sort(
            key=lambda m: (m.link_type == "direct", m.title_mentions > 0, m.relevance, m.mention_count),
            reverse=True,
        )
        direct_matches = [m for m in company_matches if m.link_type == "direct"]

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
        for cm in direct_matches:
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
        best_company_rel = max((m.relevance for m in company_matches), default=0.0)
        if direct_matches:
            top_c = direct_matches[0]
            # Base from top company relevance + title presence
            base = 0.5 if top_c.title_mentions > 0 else 0.3
            relevance_score = min(1.0, base + 0.1 * min(top_c.mention_count, 5))
            relevance_score = max(relevance_score, best_company_rel)
        elif market_list:
            top_m = market_list[0]
            relevance_score = min(0.7, 0.2 + 0.1 * min(top_m.mention_count, 5))
            relevance_score = max(relevance_score, best_company_rel)
        elif company_matches:
            relevance_score = best_company_rel
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
            if new_status in ("duplicate", "redundant"):
                pass  # dedup verdicts are final: only links / scores are refreshed
            elif not comp_matches and not mkt_matches and rel_score < 0.2:
                new_status = "unrelated"
            elif new_status == "unrelated" and comp_matches:
                # reclassify: a sector/peer link makes a former 'unrelated' article relevant
                new_status = "new"

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
                    "INSERT INTO article_company (article_id, company_id, mention_count, relevance_in_article,"
                    " link_type) VALUES (?, ?, ?, ?, ?)",
                    (article_id, cm.company.id, cm.mention_count, cm.relevance, cm.link_type),
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
