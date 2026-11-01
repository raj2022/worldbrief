from __future__ import annotations

import calendar
from datetime import datetime, timezone

import feedparser

from ..plugin import SourcePlugin, register
from ..text import clean

API = "https://export.arxiv.org/api/query"


@register("arxiv")
class ArxivPlugin(SourcePlugin):
    """Newest arXiv papers for a list of categories (hep-ex, cs.AI, quant-ph ...).

    arXiv asks API users to keep to about one request every 3 seconds; this
    plugin makes a single request per run.
    """

    def __init__(self, categories: list[str], **kw):
        kw.setdefault("category", "science")
        kw.setdefault("max_items", 50)
        super().__init__(**kw)
        self.categories = categories

    def parse(self, content: bytes):
        feed = feedparser.parse(content)
        out = []
        for e in feed.entries[: self.max_items]:
            t = e.get("published_parsed")
            pdf = next((l["href"] for l in e.get("links", []) if l.get("type") == "application/pdf"), None)
            authors = [a.get("name") for a in e.get("authors", [])]
            primary = (e.get("arxiv_primary_category") or {}).get("term", "")
            out.append(self.make_article(
                title=clean(e.get("title"), 300),
                url=e.get("link") or e.get("id"),
                summary=clean(e.get("summary"), 600),
                published=datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc) if t else None,
                tags=[primary] + ([f"pdf:{pdf}"] if pdf else []) +
                     ([f"authors:{', '.join(authors[:3])}{' et al.' if len(authors) > 3 else ''}"] if authors else []),
            ))
        return out

    def fetch(self):
        query = " OR ".join(f"cat:{c}" for c in self.categories)
        r = self.http_get(API, params={
            "search_query": query, "sortBy": "submittedDate",
            "sortOrder": "descending", "max_results": self.max_items,
        })
        return self.parse(r.content)
