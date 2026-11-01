"""Rolling article store kept between refreshes.

Feeds only show their last ~20–50 items, so when we refresh every few
minutes we keep what we've already seen until it ages out of its section.
This also records `first_seen`, which gives a timestamp to items whose feed
has no date and lets the app badge stories that are new since the last open.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .models import Article


class ArticleStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._items: dict[str, Article] = {}
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                for d in json.loads(self.path.read_text()):
                    a = Article.from_dict(d)
                    self._items[a.id + a.category] = a
            except (ValueError, KeyError, TypeError):
                self._items = {}  # corrupt store: start fresh rather than crash

    def __len__(self) -> int:
        return len(self._items)

    def merge(self, articles: list[Article], now: datetime) -> int:
        """Add new articles; refresh snippets for known ones. Returns # new."""
        new = 0
        with self._lock:
            for a in articles:
                key = a.id + a.category
                old = self._items.get(key)
                if old is None:
                    a.first_seen = now
                    self._items[key] = a
                    new += 1
                else:
                    a.first_seen = old.first_seen
                    if not a.summary:
                        a.summary = old.summary
                    self._items[key] = a
        return new

    def prune(self, max_age_by_section: dict[str, float], now: datetime, default_hours: float = 36) -> int:
        with self._lock:
            before = len(self._items)
            self._items = {
                k: a for k, a in self._items.items()
                if (a.when or now) >= now - timedelta(hours=max_age_by_section.get(a.category, default_hours))
            }
            return before - len(self._items)

    def all(self) -> list[Article]:
        with self._lock:
            return list(self._items.values())

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([a.to_dict() for a in self.all()], ensure_ascii=False))
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written store
