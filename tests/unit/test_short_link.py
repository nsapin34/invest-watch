"""Unit tests for short_link generation, repository and Markdown formatting."""

import pytest

from invest_watch.exceptions import DuplicateEntityError, NotFoundError
from invest_watch.short_link import (
    ShortLink,
    ShortLinkRepository,
    format_inline_code,
    format_markdown_link,
    generate_short_code,
)


class TestShortCodeGeneration:
    def test_generate_default_length(self):
        code = generate_short_code()
        assert len(code) == 6
        assert code.replace("-", "").replace("_", "").isalnum()

    def test_generate_custom_length(self):
        for length in (6, 7, 8):
            code = generate_short_code(length=length)
            assert len(code) == length

    def test_invalid_length_raises(self):
        with pytest.raises(ValueError):
            generate_short_code(5)
        with pytest.raises(ValueError):
            generate_short_code(9)

    def test_randomness(self):
        codes = {generate_short_code() for _ in range(100)}
        assert len(codes) == 100


class TestMarkdownFormatting:
    def test_format_markdown_link(self):
        assert format_markdown_link("a12bc3", "https://example.com/article") == "[a12bc3](https://example.com/article)"

    def test_format_inline_code(self):
        assert format_inline_code("a12bc3") == "[a12bc3]"


class TestShortLinkRepository:
    def test_create_and_get_by_code(self, db):
        repo = ShortLinkRepository(db)
        link = repo.create("https://example.com/news/1")

        assert len(link.short_code) == 6
        assert link.url_original == "https://example.com/news/1"

        fetched = repo.get_by_code(link.short_code)
        assert fetched is not None
        assert fetched.url_original == link.url_original

    def test_create_idempotent_for_same_url(self, db):
        repo = ShortLinkRepository(db)
        link1 = repo.create("https://example.com/news/dup")
        link2 = repo.create("https://example.com/news/dup")

        assert link1.short_code == link2.short_code

    def test_attach_article_id_on_existing_link(self, db):
        from invest_watch.article_repository import ArticleRepository

        article_repo = ArticleRepository(db)
        run = article_repo.create_run("2026-09-13", "2026-09-12T00:00:00Z", "2026-09-12T23:59:59Z")
        article = article_repo.insert_candidate(
            run_id=run.id,
            title="Article Test",
            url="https://example.com/news/article-attach",
        )
        assert article is not None

        repo = ShortLinkRepository(db)
        link1 = repo.create("https://example.com/news/article-attach")
        assert link1.article_id is None

        # Re-create with real article.id attaches it
        link2 = repo.create("https://example.com/news/article-attach", article_id=article.id)
        assert link2.short_code == link1.short_code
        assert link2.article_id == article.id

        fetched = repo.get_by_article_id(article.id)
        assert fetched is not None
        assert fetched.short_code == link1.short_code

    def test_create_custom_code(self, db):
        repo = ShortLinkRepository(db)
        link = repo.create("https://example.com/news/custom", custom_code="custom01")
        assert link.short_code == "custom01"

        # Duplicate custom code on different URL raises
        with pytest.raises(DuplicateEntityError):
            repo.create("https://example.com/news/other", custom_code="custom01")

    def test_resolve_and_render_markdown(self, db):
        repo = ShortLinkRepository(db)
        link = repo.create("https://example.com/news/md", custom_code="mdcode")

        assert repo.resolve("mdcode") == "https://example.com/news/md"
        assert repo.render_markdown("mdcode") == "[mdcode](https://example.com/news/md)"

    def test_resolve_unknown_raises_not_found(self, db):
        repo = ShortLinkRepository(db)
        with pytest.raises(NotFoundError):
            repo.resolve("unknown_code")
