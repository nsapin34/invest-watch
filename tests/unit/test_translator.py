"""Microsoft Translator integration (v008) - fully offline (httpx MockTransport)."""

import json

import httpx

from invest_watch.translator import AzureTranslator, TranslationSection, build_translator, split_chunks

ZH = "\u5a01\u80dc\u63a7\u80a1\u65b0\u589e\u4e2d\u6807\u6d77\u5916\u5408\u7ea6\u3002"


def _translator(handler, **cfg):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    t = AzureTranslator(TranslationSection(enabled=True, **cfg), api_key="k", region="francecentral",
                        client=client, sleep=lambda s: None)
    return t, calls


def _ok(request):
    body = json.loads(request.content)
    return httpx.Response(200, json=[{"translations": [{"text": "FR:" + body[0]["Text"], "to": "fr"}]}])


def test_translates_article_and_sends_auth_headers():
    t, calls = _translator(_ok)
    title_fr, content_fr = t.translate_article("Wasion", ZH, "zh")
    assert title_fr == "FR:Wasion" and content_fr == "FR:" + ZH
    req = calls[0]
    assert req.headers["Ocp-Apim-Subscription-Key"] == "k"
    assert req.headers["Ocp-Apim-Subscription-Region"] == "francecentral"
    assert "to=fr" in str(req.url) and "from=zh-Hans" in str(req.url)
    assert t.translated == 1 and t.chars_used == len("Wasion") + len(ZH)


def test_only_targeted_languages_are_translated():
    t, calls = _translator(_ok)
    assert t.translate_article("Hello", "World", "en") == (None, None)
    assert t.translate_article("Bonjour", "Monde", "fr") == (None, None)
    assert t.wants("zh") and t.wants("zh-cn") and not t.wants(None)
    assert calls == []


def test_budget_stops_translation():
    t, calls = _translator(_ok, max_chars_per_run=10)
    assert t.translate("x" * 8) == "FR:" + "x" * 8
    assert t.translate("y" * 8) is None
    assert t.budget_exhausted and len(calls) == 1


def test_auth_error_disables_translator_for_the_run():
    t, calls = _translator(lambda r: httpx.Response(401, text="bad key"))
    assert t.translate("abc", "zh") is None
    assert not t.available
    assert t.translate("def", "zh") is None
    assert len(calls) == 1


def test_retry_on_429_then_success():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        return _ok(request)

    t, calls = _translator(handler)
    assert t.translate("abc", "zh") == "FR:abc"
    assert len(calls) == 2


def test_server_error_degrades_to_none():
    t, _ = _translator(lambda r: httpx.Response(500), max_retries=1)
    assert t.translate("abc", "zh") is None
    assert t.failures == 1


def test_long_text_split_into_chunks():
    text = ("\u5b81\u5fb7\u65f6\u4ee3\u3002" * 3000)
    chunks = split_chunks(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == text
    assert split_chunks("") == []
    assert split_chunks("z" * 2500, limit=1000) == ["z" * 1000, "z" * 1000, "z" * 500]


def test_article_content_is_truncated_to_max_chars_per_article():
    t, calls = _translator(_ok, max_chars_per_article=5)
    _, content_fr = t.translate_article(None, "abcdefghij", "zh")
    assert content_fr == "FR:abcde"


def test_build_translator_disabled_or_without_key(monkeypatch):
    assert build_translator(TranslationSection(enabled=False)) is None
    assert build_translator(TranslationSection(enabled=True)) is None  # no key in env
    assert build_translator(TranslationSection(enabled=True, provider="deepl")) is None
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    t = build_translator(TranslationSection(enabled=True))
    assert t is not None and t.available
    t.close()


def test_config_reads_translation_section(project):
    from invest_watch.config_manager import load_config
    path = project / "conf" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + (
        '\n[translation]\nenabled = true\nlanguages = ["ZH", "en"]\nmax_chars_per_run = 1000\n'
    ), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.translation.enabled is True
    assert cfg.translation.languages == ("zh", "en")
    assert cfg.translation.max_chars_per_run == 1000


def test_default_config_has_translation_disabled(config):
    assert config.translation.enabled is False


def test_collection_stores_french_translation(db, config):
    """End-to-end: a Chinese article is stored with title_fr / content_fr and searchable in French."""
    from pathlib import Path

    from invest_watch.article_repository import ArticleRepository
    from invest_watch.collector import run_collection
    from invest_watch.company_repository import Company, CompanyRepository
    from invest_watch.date_detector import compute_period
    from invest_watch.fetcher import Fetcher
    from invest_watch.source_repository import Source, SourceRepository

    fixtures = Path(__file__).resolve().parent.parent / "fixtures"
    rss = (fixtures / "rss" / "hkex_sample.xml").read_bytes()
    html = (fixtures / "html" / "article_chinese.html").read_bytes()

    def handler(request):
        url = str(request.url)
        if url.endswith(".xml") or "rss" in url:
            return httpx.Response(200, content=rss)
        return httpx.Response(200, content=html, headers={"content-type": "text/html; charset=utf-8"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False, rate_limit_requests_per_sec=100)
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", False)
    object.__setattr__(config.collect, "min_content_chars", 10)

    translator, _ = _translator(lambda r: httpx.Response(
        200, json=[{"translations": [{"text": "contrat traduit en francais", "to": "fr"}]}]))
    period = compute_period(config.app.timezone, run_date="2026-09-12",
                            tolerance_hours=config.collect.period_tolerance_hours)
    run = ArticleRepository(db).create_run(period.run_date.isoformat(), period.start_iso, period.end_iso,
                                           trigger="manual", llm_status="off")
    stats = run_collection(config, db, run_id=run.id, period=period, fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="", translator=translator)
    rows = db.query("SELECT language, title_fr, content_fr FROM article")
    zh_rows = [r for r in rows if (r["language"] or "").startswith("zh")]
    assert zh_rows, "fixture should yield at least one Chinese article"
    assert all(r["content_fr"] == "contrat traduit en francais" for r in zh_rows)
    assert stats.translated == len(zh_rows)
    hits = db.query("SELECT article_id FROM article_fts WHERE article_fts MATCH 'traduit'")
    assert len(hits) == len(zh_rows)
    fetcher.close()
