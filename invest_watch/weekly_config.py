"""``[weekly]`` settings (kept apart from weekly.py to avoid import cycles)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeeklySection:
    """Settings from the ``[weekly]`` TOML section."""

    use_llm: bool = True
    max_articles_per_company: int = 25
    max_chars_per_article: int = 1200
    max_input_chars: int = 30_000
    # Per-group quotas (v010 link types); unused slots go to the other groups,
    # max_articles_per_company stays the overall cap.
    quota_direct: int = 15
    quota_sector: int = 5
    quota_peer: int = 5
