"""Plugin system.

A plugin is a class that subclasses SourcePlugin and is decorated with
@register("<type>"). Any .py file dropped into worldbrief/plugins/ is
auto-loaded, and sources.yaml creates instances of plugin types by name:

    - type: rss
      name: BBC World
      url: https://feeds.bbci.co.uk/news/world/rss.xml
      category: world
      region: global
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from abc import ABC, abstractmethod
from typing import Any

from . import httpcache
from .models import Article

log = logging.getLogger(__name__)

USER_AGENT = "WorldBrief/0.1 (+free news aggregator; contact: set-your-email)"

_REGISTRY: dict[str, type["SourcePlugin"]] = {}

# Shown to readers next to every story so they know who's talking.
#   independent:  privately or reader-funded, editorially independent
#   public:       publicly funded broadcaster/agency with editorial independence
#                 (BBC, DW, ABC Australia, NHK, Yonhap ...)
#   state_linked: government-owned or government-aligned ownership; kept only
#                 where it adds a perspective, and always labelled
#   aggregator:   an index/search source (GDELT, Hacker News); ownership varies per link
# Outright state propaganda outlets (RT, Sputnik, Xinhua, CGTN, KCNA ...) are
# not included at all.
OWNERSHIP = {"independent", "public", "state_linked", "aggregator"}


def register(type_name: str):
    def deco(cls):
        if type_name in _REGISTRY:
            raise ValueError(f"plugin type {type_name!r} registered twice")
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls
    return deco


def available_types() -> list[str]:
    load_builtin_plugins()
    return sorted(_REGISTRY)


def load_builtin_plugins() -> None:
    from . import plugins as pkg
    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{mod.name}")


def create(config: dict[str, Any]) -> "SourcePlugin":
    load_builtin_plugins()
    cfg = dict(config)
    type_name = cfg.pop("type")
    if type_name not in _REGISTRY:
        raise KeyError(f"unknown plugin type {type_name!r}; have {sorted(_REGISTRY)}")
    return _REGISTRY[type_name](**cfg)


class SourcePlugin(ABC):
    type_name: str = ""

    def __init__(self, name: str, category: str = "world", region: str = "global",
                 topic: str = "", lang: str = "en", enabled: bool = True,
                 max_items: int = 30, timeout: float = 15.0,
                 ownership: str = "independent", note: str = "",
                 limited_in: list[str] | None = None, **extra: Any):
        if ownership not in OWNERSHIP:
            raise ValueError(f"{name}: ownership must be one of {sorted(OWNERSHIP)}")
        self.name = name
        self.ownership = ownership
        self.note = note
        # Country codes where the site limits free reading (e.g. bbc.com in the
        # US since June 2025). The app can deprioritise these for readers there.
        self.limited_in = [c.upper() for c in (limited_in or [])]
        self.category = category
        self.region = region
        self.topic = topic
        self.lang = lang
        self.enabled = enabled
        self.max_items = max_items
        self.timeout = timeout
        self.extra = extra

    # Subclasses implement this. Raise on failure; the pipeline isolates errors.
    @abstractmethod
    def fetch(self) -> list[Article]: ...

    # Shared HTTP helper so every plugin is polite and testable (tests patch it).
    def http_get(self, url: str, params: dict | None = None):
        """GET with ETag/Last-Modified revalidation, so frequent polling
        costs publishers almost nothing when a feed hasn't changed."""
        return httpcache.SHARED.get(url, params, {"User-Agent": USER_AGENT}, self.timeout)

    def make_article(self, **kw) -> Article:
        kw.setdefault("source", self.name)
        kw.setdefault("category", self.category)
        kw.setdefault("region", self.region)
        kw.setdefault("topic", self.topic)
        kw.setdefault("ownership", self.ownership)
        kw.setdefault("limited_in", list(self.limited_in))
        kw.setdefault("lang", self.lang)
        kw.setdefault("plugin", self.type_name)
        return Article(**kw)

    def info(self) -> dict:
        """Public description for the app's 'Our sources' page."""
        return {"name": self.name, "type": self.type_name, "section": self.category,
                "topic": self.topic, "region": self.region, "lang": self.lang,
                "ownership": self.ownership, "note": self.note,
                "limited_in": self.limited_in}

    def __repr__(self) -> str:
        return f"<{self.type_name}:{self.name}>"
