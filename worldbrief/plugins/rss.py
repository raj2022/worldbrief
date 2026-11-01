from __future__ import annotations

import calendar
from datetime import datetime, timezone

import feedparser

from ..plugin import SourcePlugin, register
from ..text import clean


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


@register("rss")
class RSSPlugin(SourcePlugin):
    """Any RSS/Atom feed. Covers most free outlets worldwide.

    Optional filters, for outlets that mix content in one feed:
      link_contains:  keep only links containing any of these (e.g. ["/opinions/"])
      link_excludes:  drop links containing any of these (e.g. ["/live/", "/video/"])
      author:         when the feed carries bylines, show them (columns)
    """

    def __init__(self, url: str, link_contains: list[str] | None = None,
                 link_excludes: list[str] | None = None, **kw):
        super().__init__(**kw)
        self.url = url
        self.link_contains = link_contains or []
        self.link_excludes = link_excludes or []

    def _wanted(self, link: str) -> bool:
        if self.link_contains and not any(s in link for s in self.link_contains):
            return False
        return not any(s in link for s in self.link_excludes)

    def parse(self, content: bytes) -> list:
        feed = feedparser.parse(content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"unparseable feed: {feed.bozo_exception}")
        out = []
        for e in feed.entries:
            if len(out) >= self.max_items:
                break
            link = e.get("link")
            title = clean(e.get("title"), 300)
            if not link or not title or not self._wanted(link):
                continue
            tags = [t.get("term") for t in e.get("tags", []) if t.get("term")][:5]
            author = clean(e.get("author"), 80)
            if author:
                tags.append(f"author:{author}")
            out.append(self.make_article(
                title=title,
                url=link,
                summary=clean(e.get("summary") or e.get("description")),
                published=_parse_date(e),
                tags=tags,
            ))
        return out

    def fetch(self):
        return self.parse(self.http_get(self.url).content)
