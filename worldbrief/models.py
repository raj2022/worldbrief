from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# Query params that only track clicks; stripped so the same article dedupes.
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "fbclid", "gclid", "ref", "at_medium", "at_campaign",
                    "cmpid", "ocid"}


def canonical_url(url: str) -> str:
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING_PARAMS]
    host = p.netloc.lower().removeprefix("www.")
    return urlunparse(("https", host, p.path.rstrip("/"), "", urlencode(query), ""))


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


@dataclass
class Article:
    title: str
    url: str
    source: str                      # human name, e.g. "BBC World"
    category: str                    # section id from sections.yaml: "world", "science", ...
    region: str = "global"           # "africa", "asia", "europe", ...
    topic: str = ""                  # subtopic within the section, e.g. "physics"
    ownership: str = ""              # independent | public | state_linked | aggregator
    limited_in: list[str] = field(default_factory=list)  # countries where free reading is limited
    summary: str = ""                # snippet from the feed (never full text)
    published: datetime | None = None
    lang: str = "en"
    tags: list[str] = field(default_factory=list)
    plugin: str = ""                 # which plugin produced it
    first_seen: datetime | None = None   # when our pipeline first saw it

    @property
    def when(self) -> datetime | None:
        """Best guess at when this appeared: publisher date, else first seen."""
        return self.published or self.first_seen

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        for k in ("published", "first_seen"):
            if d.get(k):
                d[k] = datetime.fromisoformat(d[k])
        return cls(**d)

    @property
    def id(self) -> str:
        return hashlib.sha1(canonical_url(self.url).encode()).hexdigest()[:16]

    @property
    def domain(self) -> str:
        return domain_of(self.url)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        d["domain"] = self.domain
        for k in ("published", "first_seen"):
            v = getattr(self, k)
            d[k] = v.astimezone(timezone.utc).isoformat() if v else None
        return d
