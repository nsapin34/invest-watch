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


# -- v007: PDF extraction ------------------------------------------------------

def _make_pdf(text: str) -> bytes:
    """Minimal one-page PDF with a Helvetica text line (no extra deps)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def test_clean_pdf_extracts_text():
    from invest_watch.cleaner import clean_pdf, is_pdf
    pdf = _make_pdf("CATL announces share buyback progress")
    assert is_pdf(pdf)
    doc = clean_pdf(pdf, min_content_chars=10)
    assert "CATL announces share buyback" in doc.text
    assert doc.content_hash and not doc.metadata_only


def test_clean_pdf_garbage_never_raises():
    from invest_watch.cleaner import clean_pdf
    doc = clean_pdf(b"%PDF-1.4 garbage", min_content_chars=10)
    assert doc.text == "" and doc.metadata_only


def test_is_pdf_by_content_type_or_url():
    from invest_watch.cleaner import is_pdf
    assert is_pdf(b"", "application/pdf")
    assert is_pdf(b"", None, "https://www1.hkexnews.hk/x/2026100801007_c.pdf")
    assert not is_pdf(b"<html>", "text/html", "https://example.com/a.html")
