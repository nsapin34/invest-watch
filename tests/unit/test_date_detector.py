"""Date detection and D-1 window with timezone + ±6 h tolerance."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from invest_watch.date_detector import compute_period, detect_date, parse_datetime

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "html"


def test_period_europe_paris_d1():
    period = compute_period("Europe/Paris", run_date="2026-09-12", tolerance_hours=6)
    assert period.run_date.isoformat() == "2026-09-12"
    # 2026-09-11 00:00 CEST (UTC+2) → 2026-09-10 22:00Z
    assert period.start_utc == datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    assert period.end_utc == datetime(2026, 9, 11, 21, 59, 59, tzinfo=timezone.utc)


def test_period_contains_with_tolerance():
    period = compute_period("Europe/Paris", run_date="2026-09-12", tolerance_hours=6)
    inside = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    early_ok = datetime(2026, 9, 10, 17, 0, tzinfo=timezone.utc)  # within -6h of 22:00Z
    too_early = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    unknown = None
    assert period.contains(inside)
    assert period.contains(early_ok)
    assert not period.contains(too_early)
    assert period.contains(unknown)


def test_jsonld_date_is_certain():
    html = (FIXTURES / "article_full.html").read_text(encoding="utf-8")
    guess = detect_date(html=html, tz_name="Europe/Paris")
    assert guess.date_certaine
    assert guess.published_at is not None
    assert guess.published_at.astimezone(timezone.utc) == datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    assert guess.source and guess.source.startswith("jsonld")


def test_meta_date_paywall_page():
    html = (FIXTURES / "article_paywall.html").read_text(encoding="utf-8")
    guess = detect_date(html=html, tz_name="Europe/Paris")
    assert guess.date_certaine
    assert guess.published_at is not None


def test_chinese_meta_timezone_converted_to_utc():
    html = (FIXTURES / "article_chinese.html").read_text(encoding="utf-8")
    guess = detect_date(html=html, tz_name="Europe/Paris")
    assert guess.published_at is not None
    # 2026-09-11 16:00 +08 → 08:00 UTC
    assert guess.published_at.astimezone(timezone.utc).hour == 8


def test_rss_struct_is_certain():
    struct = (2026, 9, 11, 8, 0, 0, 0, 0, 0)
    guess = detect_date(rss_struct=struct, tz_name="Europe/Paris")
    assert guess.date_certaine
    assert guess.source == "rss.parsed"


def test_relative_date_is_uncertain():
    base = datetime(2026, 9, 12, 7, 0, tzinfo=ZoneInfo("Europe/Paris"))
    dt = parse_datetime("2 hours ago", tz_name="Europe/Paris", relative_base=base)
    assert dt is not None
    guess = detect_date(rss_published="2 hours ago", tz_name="Europe/Paris", relative_base=base)
    assert guess.published_at is not None
    assert guess.date_certaine is False


def test_unknown_date_uncertain():
    guess = detect_date(html="<html><body>no date</body></html>", tz_name="Europe/Paris")
    assert guess.published_at is None
    assert guess.date_certaine is False
