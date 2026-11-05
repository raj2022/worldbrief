"""Group articles about the same event into one story.

Lightweight and dependency-free: titles become keyword sets, and an article
joins a story when its keywords overlap enough with the story's and they name
the same people or places. A rare shared name lowers the overlap needed. Good
enough to merge "15 outlets covering the same election"; swap in embeddings
later if you want better recall.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import Article

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]+")
STOPWORDS = set("""
a an the and or but if of to in on at for from by with about as into over after before
is are was were be been being has have had do does did will would can could should may might
this that these those it its he she they them his her their our we you your i not no new says
said say amid after up down out more than first last over under how why what who when where
live latest update updates news report reports video watch
""".split())

MAX_GAP = timedelta(hours=72)     # a story that goes quiet this long doesn't continue
RELAXED_THRESHOLD = 0.4           # when two headlines share a rare name
RARE_NAME_TITLES = 12             # headlines in a section naming it; "Trump" is far over this
STRICT_SECTIONS = {"sports"}      # teams recur across different matches, so a name proves little
# Fixture-listing wording shared by every match preview, so it proves nothing.
SPORTS_BOILERPLATE = frozenset("""
streaming stream channel channels kick-off kickoff time where watch confirmed starting
lineup lineups line-up line-ups clash preview prediction predictions highlight highlights
live score scores result results today fixture fixtures team teams news odds tip tips
""".split())
PAPER_PLUGINS = {"arxiv", "journal"}
PAPER_WEIGHT = 0.6                # so reporting leads a section, and papers fill its topics
_NAME = re.compile(r"[A-Z](?:\.?[\w'’-])*\.?")
_CALENDAR = set("""
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday
""".split())


def keywords(title: str) -> frozenset[str]:
    words = (w.strip("'-") for w in _WORD.findall(title.lower()))
    return frozenset(w[:-1] if w.endswith("s") and len(w) > 4 else w
                     for w in words if w and w not in STOPWORDS and len(w) > 2)


def names(title: str) -> frozenset[str]:
    """Capitalised words, roughly the people and places a headline is about.
    All-caps and title-case headlines just give more of them, which only makes
    two headlines likelier to pass the check in `cluster`."""
    found = set()
    for word in _NAME.findall(title):
        word = re.sub(r"['’]s$", "", word.lower()).replace(".", "")
        if word not in STOPWORDS and word not in _CALENDAR:
            found.add(word[:4])       # so German matches Germany, Israeli matches Israel
    return frozenset(found)


@dataclass
class Story:
    category: str
    articles: list[Article] = field(default_factory=list)
    terms: set[str] = field(default_factory=set)     # union: a cheap pre-filter only
    names: set[str] = field(default_factory=set)
    # Each member's own keywords and names. A new headline must match one member,
    # not the union, which only grows and so drifts: "Ukraine drone strike" then
    # "Ukraine football match" would otherwise chain into one story.
    members: list[tuple[frozenset[str], frozenset[str]]] = field(default_factory=list)

    def add(self, a: Article, kw: frozenset[str], who: frozenset[str] = frozenset()) -> None:
        self.articles.append(a)
        self.terms |= kw
        self.names |= who
        self.members.append((kw, who))

    @property
    def lead(self) -> Article:
        # Prefer the article with a snippet, then the earliest report.
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        return min(self.articles, key=lambda a: (not a.summary, a.when or far_future))

    @property
    def sources(self) -> list[str]:
        """Distinct outlets, by publisher rather than feed name."""
        return sorted({a.outlet for a in self.articles})

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
        weight = PAPER_WEIGHT if self.articles[0].plugin in PAPER_PLUGINS else 1
        return weight * (len(self.sources) + 0.5 * (len(self.regions) - 1)) * (0.3 + freshness)

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
    titles_naming = Counter((a.category, n) for a in articles for n in names(a.title))
    for a in sorted(articles, key=lambda x: x.when or epoch):
        kw = keywords(a.title)
        who = names(a.title)
        if a.category in STRICT_SECTIONS:
            kw -= SPORTS_BOILERPLATE
            who -= {w[:4] for w in SPORTS_BOILERPLATE}
        best, best_sim = None, 0.0
        # Papers are each their own item; only news clusters.
        if a.plugin not in PAPER_PLUGINS:
            for s in stories:
                if s.category != a.category or s.articles[0].plugin in PAPER_PLUGINS:
                    continue
                # The union is a superset of every member, so failing here means
                # no member can match either.
                if len(kw & s.terms) < min_shared:
                    continue
                if a.when and s.latest and a.when - s.latest > MAX_GAP:
                    continue
                for mkw, mwho in s.members:
                    # Every title needs min_shared words in common, however short it
                    # is. Otherwise "Healthcare for all" joins any story about healthcare.
                    if len(kw & mkw) < min_shared:
                        continue
                    # Shared words aren't enough if the headlines name different people
                    # and places: "October worst on record" for Russia and for hospitals.
                    if who and mwho and not who & mwho:
                        continue
                    sim = similarity(kw, mkw)
                    # "Binance" pins down a story, so less of the headline has to match.
                    if a.category not in STRICT_SECTIONS and any(
                            titles_naming[(a.category, n)] <= RARE_NAME_TITLES for n in who & mwho):
                        sim += threshold - RELAXED_THRESHOLD
                    if sim > best_sim:
                        best, best_sim = s, sim
        if best is not None and best_sim >= threshold:
            best.add(a, kw, who)
        else:
            s = Story(category=a.category)
            s.add(a, kw, who)
            stories.append(s)
    return stories
