"""Opinion-related plugins, source labels and access limits."""
import json
from pathlib import Path

import pytest

from worldbrief import plugin, pipeline
from worldbrief.catalog import Catalog
from worldbrief.httpcache import CachedResponse

ROOT = Path(__file__).parent.parent
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Why the ceasefire may not hold</title><link>https://www.aljazeera.com/opinions/2026/11/1/x</link>
<author>Jane Doe</author><pubDate>Sun, 01 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>Ceasefire talks resume</title><link>https://www.aljazeera.com/news/2026/11/1/y</link>
<pubDate>Sun, 01 Nov 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


def fixed(p, payload: bytes):
    p.http_get = lambda *a, **k: CachedResponse(payload, 200)
    return p


def test_rss_link_filters_split_one_feed_into_news_and_opinion():
    opinion = fixed(plugin.create({"type": "rss", "name": "AJ Opinion", "url": "x",
                                   "link_contains": ["/opinions/"], "category": "opinion",
                                   "topic": "columns"}), RSS)
    news = fixed(plugin.create({"type": "rss", "name": "AJ", "url": "x",
                                "link_excludes": ["/opinions/"]}), RSS)
    [o] = opinion.fetch()
    [n] = news.fetch()
    assert o.title.startswith("Why the ceasefire") and "author:Jane Doe" in o.tags
    assert n.title == "Ceasefire talks resume"


def test_substack_keeps_only_free_posts():
    archive = [
        {"title": "Free post", "canonical_url": "https://x.substack.com/p/free", "audience": "everyone",
         "post_date": "2026-10-31T12:00:00.000Z", "subtitle": "Open to all"},
        {"title": "Paid post", "canonical_url": "https://x.substack.com/p/paid", "audience": "only_paid",
         "post_date": "2026-10-31T13:00:00.000Z"},
        {"title": "Founders", "canonical_url": "https://x.substack.com/p/f", "audience": "founding"},
    ]
    p = fixed(plugin.create({"type": "substack", "name": "X", "site": "https://x.substack.com",
                             "topic": "newsletters"}), json.dumps(archive).encode())
    arts = p.fetch()
    assert [a.title for a in arts] == ["Free post"]
    assert arts[0].category == "opinion" and arts[0].summary == "Open to all"


def test_substack_falls_back_to_rss_when_archive_fails():
    p = plugin.create({"type": "substack", "name": "X", "site": "https://x.substack.com",
                       "topic": "newsletters"})
    calls = []

    def get(url, params=None):
        calls.append(url)
        if url.endswith("/archive"):
            raise ConnectionError("blocked")
        return CachedResponse(RSS, 200)

    p.http_get = get
    assert len(p.fetch()) == 2 and calls[-1] == "https://x.substack.com/feed"


def test_hf_daily_papers_ranked_by_upvotes_and_link_to_arxiv():
    items = [
        {"paper": {"id": "2610.00001", "title": "Small", "upvotes": 2, "publishedAt": "2026-10-31T00:00:00Z"}},
        {"paper": {"id": "2610.00002", "title": "Popular", "upvotes": 90, "summary": "s",
                   "publishedAt": "2026-10-31T00:00:00Z", "authors": [{"name": "A"}]}},
        {"paper": {"id": "2610.00003", "title": "Medium", "upvotes": 20, "publishedAt": "2026-10-31T00:00:00Z"}},
    ]
    p = fixed(plugin.create({"type": "hf_papers", "name": "HF"}), json.dumps(items).encode())
    arts = p.fetch()
    assert [a.title for a in arts] == ["Popular", "Medium"]
    assert arts[0].url == "https://arxiv.org/abs/2610.00002" and arts[0].topic == "ai"
    assert arts[0].ownership == "aggregator"


def test_ownership_and_access_labels_flow_to_articles():
    p = fixed(plugin.create({"type": "rss", "name": "BBC", "url": "x", "ownership": "public",
                             "limited_in": ["us"], "note": "UK public broadcaster"}), RSS)
    a = p.fetch()[0]
    assert a.ownership == "public" and a.limited_in == ["US"]
    assert p.info()["note"] == "UK public broadcaster"
    with pytest.raises(ValueError):
        plugin.create({"type": "rss", "name": "Bad", "url": "x", "ownership": "propaganda"})


def test_real_sources_file_has_labels_and_viewpoint_range():
    plugins = pipeline.load_sources(ROOT / "sources.yaml", Catalog.from_file(ROOT / "sections.yaml"))
    names = {p.name: p for p in plugins}
    assert names["BBC World"].limited_in == ["US"]
    assert names["Al Jazeera"].ownership == "state_linked"
    # no excluded state-propaganda outlets slipped in
    banned = ("rt.com", "sputnik", "xinhua", "cgtn", "globaltimes", "presstv", "telesur", "kcna")
    urls = " ".join(str(getattr(p, "url", "")) + str(getattr(p, "site", "")) for p in plugins).lower()
    assert not any(b in urls for b in banned)
    # opinion spans the spectrum
    opinion = [p for p in plugins if p.category == "opinion"]
    notes = " ".join(p.note.lower() for p in opinion)
    for leaning in ("left", "centre", "right", "libertarian"):
        assert leaning in notes, leaning
    assert {p.region for p in opinion} >= {"africa", "asia", "europe", "americas", "oceania"}
    assert {p.topic for p in opinion} == Catalog.from_file(ROOT / "sections.yaml").by_id["opinion"].subtopic_ids
