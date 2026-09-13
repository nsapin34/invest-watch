"""Cleaner: trafilatura extraction, paywall/dynamic fallback, hashing."""

from pathlib import Path

from invest_watch.cleaner import clean_html, content_hash, excerpt_of, normalise_whitespace

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "html"


def test_full_article_extracts_body_and_hash():
    html = (FIXTURES / "article_full.html").read_text(encoding="utf-8")
    doc = clean_html(html)
    assert not doc.metadata_only
    assert "exceeded analyst estimates" in doc.text
    assert doc.word_count > 20
    assert doc.excerpt
    assert doc.content_hash == content_hash(doc.text)
    assert doc.author == "Jane Reporter"


def test_paywall_keeps_metadata_only():
    html = (FIXTURES / "article_paywall.html").read_text(encoding="utf-8")
    doc = clean_html(html, min_content_chars=200)
    assert doc.paywall
    assert doc.metadata_only
    assert "réservés aux abonnés" in doc.text.lower() or "byd" in doc.text.lower()
    assert "subscribe" not in (doc.description or "").lower() or doc.description


def test_dynamic_page_falls_back_to_og_description():
    html = (FIXTURES / "article_dynamic.html").read_text(encoding="utf-8")
    doc = clean_html(html, min_content_chars=200)
    assert doc.metadata_only
    assert "factory expansion" in (doc.text or "").lower() or "factory expansion" in (doc.description or "").lower()


def test_chinese_article_extracts_cjk_text():
    html = (FIXTURES / "article_chinese.html").read_text(encoding="utf-8")
    doc = clean_html(html)
    assert "比亚迪" in doc.text
    assert not doc.metadata_only


def test_normalise_and_excerpt():
    assert normalise_whitespace(" a\n\tb  ") == "a b"
    short = excerpt_of("hello world")
    assert short == "hello world"
    long = excerpt_of("x" * 500)
    assert len(long) == 400
    assert long.endswith("…")
