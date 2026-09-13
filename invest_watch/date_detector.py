"""Publication date detection: JSON-LD, meta tags, RSS, then dateparser.

Everything is stored in UTC. ``dateparser`` is always given the configured
timezone so relative phrases ("2 hours ago") are never parsed naively
(specs §6.2–§6.3, architecture §4).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import dateparser
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_DATE_META_KEYS = (
    "article:published_time",
    "og:published_time",
    "og:updated_time",
    "pubdate",
    "publishdate",
    "date",
    "dc.date",
    "dc.date.issued",
    "sailthru.date",
)


@dataclass(frozen=True)
class DateGuess:
    """A publication datetime in UTC plus whether the source was authoritative."""

    published_at: datetime | None
    date_certaine: bool
    source: str | None = None

    @property
    def iso(self) -> str | None:
        if self.published_at is None:
            return None
        return self.published_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CollectionPeriod:
    """D-1 interval in UTC, plus the run date in the user timezone."""

    run_date: date
    start_utc: datetime
    end_utc: datetime
    timezone: str
    tolerance: timedelta

    @property
    def start_iso(self) -> str:
        return self.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def end_iso(self) -> str:
        return self.end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    def contains(self, published: datetime | None) -> bool:
        """Unknown dates are kept (``date_certaine = false`` later)."""
        if published is None:
            return True
        instant = published.astimezone(timezone.utc)
        return self.start_utc - self.tolerance <= instant <= self.end_utc + self.tolerance


def compute_period(
    tz_name: str,
    *,
    run_date: date | str | None = None,
    now: datetime | None = None,
    tolerance_hours: int = 6,
) -> CollectionPeriod:
    """D-1 00:00–23:59:59 in ``tz_name``, converted to UTC (workflow step 1)."""
    tz = ZoneInfo(tz_name)
    if isinstance(run_date, str):
        run_date = date.fromisoformat(run_date.replace("/", "-")[:10])
    if run_date is None:
        local_now = now.astimezone(tz) if now is not None else datetime.now(tz)
        run_date = local_now.date()
    day = run_date - timedelta(days=1)
    start_local = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end_local = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=tz)
    return CollectionPeriod(
        run_date=run_date,
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
        timezone=tz_name,
        tolerance=timedelta(hours=tolerance_hours),
    )


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_datetime(raw: str | None, *, tz_name: str, relative_base: datetime | None = None) -> datetime | None:
    """Parse an explicit or relative date string into an aware UTC datetime."""
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return to_utc(parsed)
    except (TypeError, ValueError, IndexError):
        pass
    settings: dict[str, Any] = {
        "TIMEZONE": tz_name,
        "TO_TIMEZONE": "UTC",
        "RETURN_AS_TIMEZONE_AWARE": True,
    }
    if relative_base is not None:
        settings["RELATIVE_BASE"] = relative_base.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
    try:
        result = dateparser.parse(text, settings=settings)
    except Exception as exc:  # noqa: BLE001
        logger.debug("dateparser failed on %r: %s", text[:80], exc)
        return None
    if result is None:
        return None
    return to_utc(result)


def _is_relative(text: str) -> bool:
    return bool(
        re.search(
            r"\b(ago|yesterday|today|tomorrow|hier|aujourd.?hui|il y a|前|昨天|今天)\b",
            text,
            re.IGNORECASE,
        )
    )


def _from_json_ld(html: str, tz_name: str) -> DateGuess | None:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _walk_json(payload):
            for key in ("datePublished", "dateCreated", "dateModified"):
                value = node.get(key) if isinstance(node, dict) else None
                if isinstance(value, str):
                    dt = parse_datetime(value, tz_name=tz_name)
                    if dt is not None:
                        return DateGuess(dt, True, source=f"jsonld.{key}")
    return None


def _walk_json(node: Any) -> list[Any]:
    if isinstance(node, list):
        items: list[Any] = []
        for item in node:
            items.extend(_walk_json(item))
        return items
    if isinstance(node, dict):
        graph = node.get("@graph")
        if graph is not None:
            return _walk_json(graph) + [node]
        return [node]
    return []


def _from_meta(html: str, tz_name: str) -> DateGuess | None:
    soup = BeautifulSoup(html, "lxml")
    for key in _DATE_META_KEYS:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag is None:
            continue
        content = tag.get("content")
        if not content:
            continue
        dt = parse_datetime(str(content), tz_name=tz_name)
        if dt is not None:
            certain = not _is_relative(str(content))
            return DateGuess(dt, certain, source=f"meta.{key}")
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag is not None:
        dt = parse_datetime(str(time_tag.get("datetime")), tz_name=tz_name)
        if dt is not None:
            return DateGuess(dt, True, source="time.datetime")
    return None


def detect_date(
    *,
    html: str | None = None,
    rss_published: str | None = None,
    rss_struct: tuple | None = None,
    tz_name: str = "Europe/Paris",
    relative_base: datetime | None = None,
) -> DateGuess:
    """Resolve a publication date. Order: JSON-LD → meta → RSS → dateparser."""
    if html:
        found = _from_json_ld(html, tz_name)
        if found is not None:
            return found
        found = _from_meta(html, tz_name)
        if found is not None:
            return found

    if rss_struct:
        try:
            # feedparser: time.struct_time in UTC when available
            dt = datetime(*rss_struct[:6], tzinfo=timezone.utc)
            if dt.year >= 1970:
                return DateGuess(dt, True, source="rss.parsed")
        except (TypeError, ValueError):
            pass

    if rss_published:
        dt = parse_datetime(rss_published, tz_name=tz_name, relative_base=relative_base)
        if dt is not None:
            return DateGuess(dt, not _is_relative(rss_published), source="rss.published")

    return DateGuess(None, False, source=None)
