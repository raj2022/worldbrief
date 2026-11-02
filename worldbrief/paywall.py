"""Drop links that readers can't open for free.

Curated sources in sources.yaml are already free outlets; this filter mainly
protects against open-ended plugins (GDELT, Hacker News) that can return links
to any publisher. Lists live in paywall.yaml so you can edit them without code.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .models import Article

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "paywall.yaml"


class PaywallFilter:
    def __init__(self, blocked: set[str], allowed: set[str] | None = None):
        self.blocked = {d.lower().removeprefix("www.") for d in blocked}
        self.allowed = {d.lower().removeprefix("www.") for d in (allowed or set())}

    @classmethod
    def from_file(cls, path: Path = DEFAULT_FILE) -> "PaywallFilter":
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls(set(data.get("blocked", [])), set(data.get("allowed", [])))

    @staticmethod
    def _matches(domain: str, pool: set[str]) -> bool:
        # "ft.com" also matches "markets.ft.com", but not "craft.com".
        return any(domain == d or domain.endswith("." + d) for d in pool)

    def is_free(self, article: Article) -> bool:
        # Journals are chosen by hand, and their abstract pages are free to read.
        # Their publishers stay blocked for open-ended plugins like Hacker News.
        if article.plugin == "journal":
            return True
        d = article.domain
        if self._matches(d, self.allowed):
            return True
        return not self._matches(d, self.blocked)

    def apply(self, articles: list[Article]) -> tuple[list[Article], list[Article]]:
        keep, dropped = [], []
        for a in articles:
            (keep if self.is_free(a) else dropped).append(a)
        return keep, dropped
