"""Group articles about the same event into one story.

Lightweight and dependency-free: titles become keyword sets, and an article
joins a story when its keywords overlap enough with the story's. Good enough
to merge "15 outlets covering the same election"; swap in embeddings later
if you want better recall.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Article

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]+")
STOPWORDS = set("""
a an the and or but if of to in on at for from by with about as into over after before
is are was were be been being has have had do does did will would can could should may might
this that these those it its he she they them his her their our we you your i not no new says
said say amid after up down out more than first last over under how why what who when where
live latest update updates news report reports video watch
""".split())


def keywords(title: str) -> frozenset[str]:
    words = (w.strip("'-") for w in _WORD.findall(title.lower()))
    return frozenset(w[:-1] if w.endswith("s") and len(w) > 4 else w
                     for w in words if w and w not in STOPWORDS and len(w) > 2)


@dataclass
class Story:
    category: str
    articles: list[Article] = field(default_factory=list)
    terms: set[str] = field(default_factory=set)

    def add(self, a: Article, kw: frozenset[str]) -> None:
        self.articles.append(a)
        self.terms |= kw

    @property
    def lead(self) -> Article:
        # Prefer the article with a snippet, then the earliest report.
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        return min(self.articles, key=lambda a: (not a.summary, a.when or far_future))

    @property
    def sources(self) -> list[str]:
        return sorted({a.source for a in self.articles})

    @property
    def regions(self) -> list[str]:
        return sorted({a.region for a in self.articles})

    @property
    def topics(self) -> list[str]:
        return sorted({a.topic for a in self.articles if a.topic})

    @property
    def latest(self) -> datetime | None:
        ds = [a.when for a in self.articles if a.when]
        return max(ds) if ds else None

    def score(self, now: datetime, decay_hours: float = 24) -> float:
        """More independent outlets + more regions + fresher = higher."""
        hours = ((now - self.latest).total_seconds() / 3600) if self.latest else 48
        freshness = math.exp(-max(hours, 0) / decay_hours)
        return (len(self.sources) + 0.5 * (len(self.regions) - 1)) * (0.3 + freshness)

    def to_dict(self, now: datetime, decay_hours: float = 24) -> dict:
        lead = self.lead
        return {
            "id": hashlib.sha1("|".join(sorted(a.id for a in self.articles)).encode()).hexdigest()[:16],
            "headline": lead.title,
            "snippet": lead.summary,
            "category": self.category,
            "regions": self.regions,
            "topics": self.topics,
            "source_count": len(self.sources),
            "latest": self.latest.isoformat() if self.latest else None,
            "score": round(self.score(now, decay_hours), 3),
            "articles": [a.to_dict() for a in
                         sorted(self.articles, key=lambda a: a.when or now, reverse=True)],
        }


def similarity(a: frozenset[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    # Overlap coefficient: robust when one headline is much shorter.
    return len(a & b) / min(len(a), len(b))


def cluster(articles: list[Article], threshold: float = 0.6, min_shared: int = 3) -> list[Story]:
    stories: list[Story] = []
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for a in sorted(articles, key=lambda x: x.when or epoch):
        kw = keywords(a.title)
        best, best_sim = None, 0.0
        # Papers are each their own item; only news clusters.
        if a.plugin != "arxiv":
            for s in stories:
                if s.category != a.category or s.articles[0].plugin == "arxiv":
                    continue
                # Every title needs min_shared words in common, however short it
                # is. Otherwise "Healthcare for all" joins any story about healthcare.
                if len(kw & s.terms) < min_shared:
                    continue
                sim = similarity(kw, s.terms)
                if sim > best_sim:
                    best, best_sim = s, sim
        if best is not None and best_sim >= threshold:
            best.add(a, kw)
        else:
            s = Story(category=a.category)
            s.add(a, kw)
            stories.append(s)
    return stories
