"""Drops items that aren't readable stories: live blogs, videos, galleries,
podcasts and recurring pages. Feeds mix these in with articles, and a headline
that opens a video player is a poor fit for a text-first news app."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .models import Article

_TITLE = re.compile(
    r"^\W*(watch|video|photos?|gallery|in pictures|live|podcast|listen)\s*[:|–—-]"
    r"|\blive updates?\b"
    r"|\blive stream(ing)?\b"
    r"|\b(where|how) to watch\b"
    r"|[–—-]\s*(video|in pictures)\s*$"
    r"|^latest news bulletin\b"
    r"|^(morning|evening) update$"
    r"|^\W*letters?\W*$"
    r"|^open thread$",
    re.IGNORECASE,
)
_PATH = re.compile(
    r"/(videos?|gallery|galleries|live|liveblog|podcasts?|pod|vid|programmes|audio)(/|$)",
    re.IGNORECASE,
)


def is_junk(article: Article) -> bool:
    return bool(_TITLE.search(article.title.strip()) or _PATH.search(urlsplit(article.url).path))


def drop_junk(articles: list[Article]) -> tuple[list[Article], list[Article]]:
    """Returns (kept, dropped)."""
    kept, dropped = [], []
    for a in articles:
        (dropped if is_junk(a) else kept).append(a)
    return kept, dropped
