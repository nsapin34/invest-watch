"""Language detection on a 300-character window (lingua + fallback)."""

from invest_watch.lang_detector import detect_language


def test_english():
    text = (
        "The company reported stronger than expected annual results and confirmed "
        "that electric vehicle demand remains robust across Europe and China."
    )
    assert detect_language(text) == "en"


def test_french():
    text = (
        "L'entreprise a publié des résultats annuels meilleurs que prévu et confirme "
        "que la demande de véhicules électriques reste solide en Europe."
    )
    assert detect_language(text) == "fr"


def test_chinese():
    text = "比亚迪股份有限公司今日公布年度业绩，新能源汽车销量继续增长，公司将扩大海外市场并投入电池研发。"
    assert detect_language(text) == "zh"


def test_too_short_returns_none():
    assert detect_language("hi") is None
    assert detect_language("") is None
    assert detect_language(None) is None


def test_normalise_code_aliases():
    from invest_watch.lang_detector import _normalise_code
    assert _normalise_code("zh-CN") == "zh"
    assert _normalise_code("zh-TW") == "zh"
    assert _normalise_code("iw") == "he"
    assert _normalise_code(None) is None
    assert _normalise_code("") is None
    assert _normalise_code("EN") == "en"
    assert _normalise_code("fr-FR") == "fr"


def test_detect_language_fallback_on_lingua_failure(monkeypatch):
    """When lingua raises, langdetect fallback is tried."""
    import invest_watch.lang_detector as ld_mod

    def _failing_detector():
        class _D:
            def detect_language_of(self, _):
                raise RuntimeError("lingua broken")
        return _D()

    monkeypatch.setattr(ld_mod, "_lingua_detector", _failing_detector)
    # langdetect should still detect English
    text = (
        "The company reported stronger than expected annual results and confirmed "
        "that electric vehicle demand remains robust across Europe and China."
    )
    result = ld_mod.detect_language(text)
    assert result == "en"

