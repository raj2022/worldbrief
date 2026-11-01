from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..plugin import SourcePlugin, register

API = "https://hacker-news.firebaseio.com/v0"


@register("hackernews")
class HackerNewsPlugin(SourcePlugin):
    """Top Hacker News stories above a score threshold (tech/CS signal)."""

    def __init__(self, min_score: int = 150, **kw):
        kw.setdefault("category", "technology")
        kw.setdefault("max_items", 20)
        kw.setdefault("ownership", "aggregator")
        super().__init__(**kw)
        self.min_score = min_score

    def to_article(self, item: dict):
        if not item or item.get("type") != "story" or item.get("score", 0) < self.min_score:
            return None
        hn = f"https://news.ycombinator.com/item?id={item['id']}"
        return self.make_article(
            title=item.get("title", ""),
            url=item.get("url") or hn,
            published=datetime.fromtimestamp(item["time"], tz=timezone.utc) if item.get("time") else None,
            tags=[f"score:{item.get('score', 0)}", f"discussion:{hn}"],
        )

    def fetch(self):
        ids = self.http_get(f"{API}/topstories.json").json()[:60]
        with ThreadPoolExecutor(8) as ex:
            items = list(ex.map(lambda i: self.http_get(f"{API}/item/{i}.json").json(), ids))
        arts = [a for a in map(self.to_article, items) if a]
        return arts[: self.max_items]
