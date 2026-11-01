from __future__ import annotations

from datetime import datetime

from ..plugin import SourcePlugin, register
from ..text import clean

API = "https://huggingface.co/api/daily_papers"


@register("hf_papers")
class HFDailyPapersPlugin(SourcePlugin):
    """Hugging Face Daily Papers: AI papers picked and upvoted by the community.

    Successor signal to Papers with Code (shut down July 2025). Each item is an
    arXiv paper, so links go to arxiv.org (always free). Upvotes are kept as a
    tag the app can show or rank by.
    """

    def __init__(self, min_upvotes: int = 5, **kw):
        kw.setdefault("category", "science")
        kw.setdefault("topic", "ai")
        kw.setdefault("max_items", 20)
        kw.setdefault("ownership", "aggregator")
        super().__init__(**kw)
        self.min_upvotes = min_upvotes

    def parse(self, items: list[dict]):
        out = []
        for it in items:
            paper = it.get("paper") or {}
            arxiv_id = paper.get("id")
            upvotes = paper.get("upvotes", it.get("upvotes", 0)) or 0
            if not arxiv_id or upvotes < self.min_upvotes:
                continue
            date = paper.get("publishedAt") or it.get("publishedAt")
            authors = [a.get("name") for a in paper.get("authors", []) if a.get("name")]
            out.append(self.make_article(
                title=clean(paper.get("title") or it.get("title"), 300),
                url=f"https://arxiv.org/abs/{arxiv_id}",
                summary=clean(paper.get("summary"), 600),
                published=datetime.fromisoformat(date.replace("Z", "+00:00")) if date else None,
                tags=[f"upvotes:{upvotes}", f"discussion:https://huggingface.co/papers/{arxiv_id}"]
                     + ([f"authors:{', '.join(authors[:3])}{' et al.' if len(authors) > 3 else ''}"] if authors else []),
            ))
        out.sort(key=lambda a: -int(a.tags[0].split(":")[1]))
        return out[: self.max_items]

    def fetch(self):
        return self.parse(self.http_get(API).json())
