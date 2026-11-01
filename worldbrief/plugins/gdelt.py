from __future__ import annotations

from datetime import datetime, timezone

from ..plugin import SourcePlugin, register
from ..text import clean

API = "https://api.gdeltproject.org/api/v2/doc/doc"


@register("gdelt")
class GDELTPlugin(SourcePlugin):
    """GDELT DOC 2.0: free, monitors news in 100+ languages worldwide.

    Good for filling regional gaps: query e.g. 'sourcecountry:nigeria election'.
    Results link to arbitrary publishers, so the pipeline's paywall filter
    matters most here.
    """

    def __init__(self, query: str, timespan: str = "24h", sourcelang: str | None = "english", **kw):
        kw.setdefault("ownership", "aggregator")
        super().__init__(**kw)
        self.query = query
        self.timespan = timespan
        self.sourcelang = sourcelang

    def parse(self, data: dict):
        out = []
        for a in (data.get("articles") or [])[: self.max_items]:
            seen = a.get("seendate")  # e.g. 20261101T083000Z
            try:
                published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) if seen else None
            except ValueError:
                published = None
            out.append(self.make_article(
                title=clean(a.get("title"), 300),
                url=a["url"],
                source=a.get("domain") or self.name,
                published=published,
                lang=(a.get("language") or self.lang)[:2].lower(),
                tags=[t for t in [a.get("sourcecountry")] if t],
            ))
        return [x for x in out if x.title]

    def fetch(self):
        q = self.query + (f" sourcelang:{self.sourcelang}" if self.sourcelang else "")
        r = self.http_get(API, params={
            "query": q, "mode": "artlist", "format": "json",
            "maxrecords": self.max_items, "timespan": self.timespan, "sort": "hybridrel",
        })
        return self.parse(r.json())
