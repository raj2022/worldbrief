from __future__ import annotations

from datetime import datetime

from ..plugin import SourcePlugin, register
from ..text import clean
from .rss import RSSPlugin


@register("substack")
class SubstackPlugin(SourcePlugin):
    """Free posts from a Substack newsletter.

    Substack's public RSS also lists paid-only posts, which readers can't open.
    The publication archive endpoint marks each post with an `audience`
    ("everyone" = free), so we use it to keep only free posts. That endpoint is
    unofficial; if it fails we fall back to the RSS feed and drop posts tagged
    as paid where the feed shows it.

        - {type: substack, name: Noahpinion, site: "https://www.noahpinion.blog", category: opinion, topic: newsletters}
    """

    def __init__(self, site: str, **kw):
        kw.setdefault("category", "opinion")
        kw.setdefault("max_items", 10)
        super().__init__(**kw)
        self.site = site.rstrip("/")

    def parse_archive(self, posts: list[dict]):
        out = []
        for p in posts:
            if p.get("audience", "everyone") != "everyone":
                continue  # paid or founding-member only
            url = p.get("canonical_url")
            title = clean(p.get("title"), 300)
            if not url or not title:
                continue
            date = p.get("post_date")
            out.append(self.make_article(
                title=title,
                url=url,
                summary=clean(p.get("subtitle") or p.get("description")),
                published=datetime.fromisoformat(date.replace("Z", "+00:00")) if date else None,
                tags=["newsletter"],
            ))
        return out[: self.max_items]

    def fetch(self):
        try:
            data = self.http_get(f"{self.site}/api/v1/archive",
                                 params={"sort": "new", "limit": self.max_items * 2}).json()
            if isinstance(data, list):
                return self.parse_archive(data)
        except Exception:  # noqa: BLE001 — unofficial endpoint; fall back to RSS
            pass
        rss = RSSPlugin(url=f"{self.site}/feed", name=self.name, category=self.category,
                        region=self.region, topic=self.topic, max_items=self.max_items)
        rss.http_get = self.http_get
        return [a for a in rss.fetch() if "paid" not in (t.lower() for t in a.tags)]
