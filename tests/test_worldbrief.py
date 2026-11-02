import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from worldbrief import plugin, pipeline
from worldbrief.catalog import Catalog
from worldbrief.cluster import cluster, keywords, names
from worldbrief.models import Article, canonical_url
from worldbrief.paywall import PaywallFilter
from worldbrief.plugin import SourcePlugin, register

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def json(self):
        return json.loads(self.content)


def offline(p, fixture: str):
    """Point a plugin's HTTP at a local fixture file."""
    data = (FIX / fixture).read_bytes()
    p.http_get = lambda *a, **k: FakeResponse(data)
    return p


# ── plugin system ──────────────────────────────────────────────

def test_builtin_types_registered():
    assert {"rss", "arxiv", "gdelt", "hackernews"} <= set(plugin.available_types())


def test_every_configured_source_instantiates_and_matches_catalog():
    catalog = Catalog.from_file(ROOT / "sections.yaml")
    plugins = pipeline.load_sources(ROOT / "sources.yaml", catalog)
    assert len(plugins) >= 70
    world_regions = {p.region for p in plugins if p.category == "world"}
    assert {"global", "africa", "asia", "middle_east", "europe", "americas", "oceania"} <= world_regions
    # every section has sources, and every science subtopic has at least one
    for s in catalog.sections:
        assert any(p.category == s.id for p in plugins), f"no sources for {s.id}"
    assert {p.topic for p in plugins if p.category == "science"} == catalog.by_id["science"].subtopic_ids


def test_catalog_rejects_mismatched_sources(tmp_path):
    bad = tmp_path / "s.yaml"
    bad.write_text("sources:\n"
                   "  - {type: rss, name: A, url: x, category: gossip}\n"
                   "  - {type: rss, name: B, url: x, category: science}\n"
                   "  - {type: rss, name: C, url: x, category: world, region: mars}\n")
    with pytest.raises(ValueError) as e:
        pipeline.load_sources(bad, Catalog.from_file(ROOT / "sections.yaml"))
    msg = str(e.value)
    assert "unknown section 'gossip'" in msg and "needs a topic" in msg and "unknown region 'mars'" in msg


def test_only_filter_limits_sections():
    plugins = pipeline.load_sources(ROOT / "sources.yaml", only={"sports", "health"})
    assert plugins and {p.category for p in plugins} == {"sports", "health"}
    with pytest.raises(ValueError):
        pipeline.load_sources(ROOT / "sources.yaml", only={"nonsense"})


def test_unknown_type_errors():
    with pytest.raises(KeyError):
        plugin.create({"type": "nope", "name": "x"})


def test_custom_plugin_can_be_added():
    @register("unit_test_static")
    class Static(SourcePlugin):
        def fetch(self):
            return [self.make_article(title="Hello world", url="https://example.org/a")]

    p = plugin.create({"type": "unit_test_static", "name": "Static", "region": "europe"})
    [a] = p.fetch()
    assert (a.source, a.region, a.plugin) == ("Static", "europe", "unit_test_static")


# ── individual plugins ─────────────────────────────────────────

def test_rss_plugin_parses_and_cleans():
    p = offline(plugin.create({"type": "rss", "name": "BBC", "url": "x"}), "bbc.xml")
    arts = p.fetch()
    assert len(arts) == 3
    assert arts[0].summary == "Millions of Brazilians vote in a closely fought runoff."
    assert arts[0].published == datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def test_arxiv_plugin():
    p = offline(plugin.create({"type": "arxiv", "name": "arXiv", "categories": ["hep-ex"]}), "arxiv.xml")
    arts = p.fetch()
    assert arts[0].title.startswith("Search for a heavy resonance decaying to a top quark pair in")
    assert arts[0].category == "science"
    assert "hep-ex" in arts[0].tags
    assert any(t.startswith("pdf:") for t in arts[0].tags)
    assert "authors:A. Author, B. Author, C. Author et al." in arts[0].tags


def test_gdelt_plugin():
    p = offline(plugin.create({"type": "gdelt", "name": "G", "query": "q", "region": "africa"}), "gdelt.json")
    arts = p.fetch()
    assert len(arts) == 3 and arts[0].source == "premiumtimesng.com" and arts[0].region == "africa"


def test_hackernews_filters_by_score():
    p = plugin.create({"type": "hackernews", "name": "HN", "min_score": 100})
    assert p.to_article({"type": "story", "id": 1, "score": 50, "title": "x", "time": 1}) is None
    a = p.to_article({"type": "story", "id": 2, "score": 300, "title": "Big", "url": "https://e.com/x", "time": 1})
    assert a.url == "https://e.com/x" and "score:300" in a.tags


# ── filtering, dedupe, clustering ──────────────────────────────

def art(title, url, source="S", region="global", hours_ago=1, category="world", plugin="rss"):
    return Article(title=title, url=url, source=source, category=category, region=region,
                   published=NOW.replace(hour=12 - hours_ago), plugin=plugin)


def test_paywall_filter():
    pw = PaywallFilter.from_file(ROOT / "paywall.yaml")
    assert not pw.is_free(art("x", "https://markets.ft.com/a"))
    assert not pw.is_free(art("x", "https://www.nytimes.com/a"))
    assert pw.is_free(art("x", "https://craft.com/a"))          # not a false match on "ft.com"
    assert pw.is_free(art("x", "https://www.bbc.co.uk/a"))
    assert pw.is_free(art("x", "https://arxiv.org/abs/1"))


def test_canonical_url_strips_tracking():
    assert canonical_url("http://www.x.com/a/?utm_source=rss&id=3") == canonical_url("https://x.com/a?id=3")


def test_cluster_merges_same_event_across_outlets():
    arts = [
        art("Brazil election: Voters head to polls in tight presidential runoff", "https://a/1", "BBC", "americas"),
        art("Brazilians vote in tight presidential election runoff", "https://b/1", "Al Jazeera", "global"),
        art("Brazil presidential runoff: polls open as voters choose", "https://c/1", "DW", "europe"),
        art("UN Security Council meets on Sudan ceasefire", "https://d/1", "UN News"),
    ]
    stories = cluster(arts)
    sizes = sorted(len(s.articles) for s in stories)
    assert sizes == [1, 3]


def test_cluster_keeps_distinct_stories_apart():
    arts = [art("Japan earthquake triggers tsunami warning", "https://a/2"),
            art("Japan election results: ruling party loses majority", "https://b/2")]
    assert len(cluster(arts)) == 2


def test_arxiv_papers_never_merge():
    arts = [art("Scaling laws for language models", "https://arxiv.org/abs/1", category="science", plugin="arxiv"),
            art("Scaling laws for language models revisited", "https://arxiv.org/abs/2", category="science", plugin="arxiv")]
    assert len(cluster(arts)) == 2


def test_cluster_needs_a_shared_name_as_well_as_shared_words():
    """A real pair from the feeds: three shared words, two different stories."""
    a = art("Russia suffers record troop losses in October, its worst month", "https://a/1")
    b = art("October worst on record for overcrowding, says INMO", "https://b/1")
    assert len(cluster([a, b])) == 2


def test_cluster_still_merges_differently_worded_names():
    a = art("Judge dismisses Trump lawsuit against Wall Street Journal", "https://a/1")
    b = art("Trump's Wall Street Journal lawsuit dismissed by judge", "https://b/1")
    c = art("U.S. judge dismisses Trump lawsuit against the Journal", "https://c/1")
    assert len(cluster([a, b, c])) == 1


def test_cluster_does_not_continue_a_story_after_days_of_silence():
    title = "Israeli strikes kill eight people in Gaza"
    def at(url, days_ago):
        return Article(title=title, url=url, source=url, category="world",
                       published=NOW - timedelta(days=days_ago))

    stories = cluster([at("https://a/1", 5), at("https://b/1", 0), at("https://c/1", 1)])
    assert sorted(len(s.articles) for s in stories) == [1, 2]


def test_sports_needs_the_full_overlap_even_with_a_rare_name():
    pair = ["New Zealand win final T20 by seven wickets but India take series",
            "Afghanistan thrash Zimbabwe to sweep T20 series after New Zealand final"]
    world = cluster([art(t, f"https://{i}/1", str(i)) for i, t in enumerate(pair)])
    sport = cluster([art(t, f"https://{i}/1", str(i), category="sports") for i, t in enumerate(pair)])
    assert len(world) == 1 and len(sport) == 2


def test_names_ignore_calendar_words_and_punctuation():
    assert names("U.S. and Iran's talks on Sunday in October") == {"us", "iran"}
    assert names("German") == names("Germany")


def test_cluster_is_looser_for_a_rare_name_than_a_common_one():
    """Real headlines. Same story, but under the normal 0.6 overlap."""
    pair = ["Ghostly Cat's Head Nebula lurks within sight of Hubble and Webb telescopes in new image",
            "Hubble and Webb spot something ghostly happening in the Cat's Head Nebula tonight in space"]
    assert len(cluster([art(pair[0], "https://a/1", "A"), art(pair[1], "https://b/1", "B")])) == 1

    common = [art(f"Trump meets governor number{i} visitor{i} guest{i} delegate{i}", f"https://c/{i}", f"O{i}")
              for i in range(13)]
    loose = [art("Trump meets governor about farming, tariffs, drought, subsidies and exports", "https://x/1", "X"),
             art("Trump meets governor before rally, fundraiser, speech, debate and interview", "https://y/1", "Y")]
    assert len(cluster(common + loose)) >= 3


def test_keywords_ignore_stopwords():
    assert keywords("The latest news on the Brazil election") == {"brazil", "election"}


# ── full pipeline ──────────────────────────────────────────────

@register("unit_test_broken")
class Broken(SourcePlugin):
    def fetch(self):
        raise ConnectionError("feed is down")


@register("unit_test_sports")
class SportsStub(SourcePlugin):
    def fetch(self):
        return [self.make_article(title="India beat Australia in World Cup semi-final",
                                  url="https://www.bbc.co.uk/sport/cricket/1",
                                  published=NOW.replace(hour=10))]


def sample_digest():
    plugins = [
        offline(plugin.create({"type": "rss", "name": "BBC World", "url": "x", "region": "global"}), "bbc.xml"),
        offline(plugin.create({"type": "rss", "name": "Al Jazeera", "url": "x", "region": "middle_east"}), "aljazeera.xml"),
        offline(plugin.create({"type": "arxiv", "name": "arXiv HEP", "categories": ["hep-ex"],
                               "topic": "physics"}), "arxiv.xml"),
        offline(plugin.create({"type": "gdelt", "name": "GDELT Africa", "query": "q", "region": "africa"}), "gdelt.json"),
        plugin.create({"type": "unit_test_sports", "name": "BBC Sport", "category": "sports"}),
        plugin.create({"type": "unit_test_broken", "name": "Dead Feed"}),
    ]
    articles, results = pipeline.fetch_all(plugins)
    return pipeline.build_digest(articles, results, PaywallFilter.from_file(ROOT / "paywall.yaml"),
                                 Catalog.from_file(ROOT / "sections.yaml"), now=NOW)


def test_end_to_end_digest_with_a_broken_source():
    digest = sample_digest()
    assert digest["schema_version"] == pipeline.SCHEMA_VERSION
    assert [c["id"] for c in digest["catalog"]][:2] == ["world", "opinion"]

    s = digest["stats"]
    assert s["sources_failed"] == 1 and s["sources_ok"] == 5
    assert s["paywalled_dropped"] == 2      # ft.com + nation.africa (paywalled since 2023)

    world = digest["sections"]["world"]
    top = world["top"][0]
    assert "Brazil" in top["headline"] and top["source_count"] == 2   # merged across outlets
    assert {"global", "middle_east"} <= set(top["regions"])
    assert "africa" in world["by_region"]

    science = digest["sections"]["science"]
    assert len(science["top"]) == 2 and len(science["by_topic"]["physics"]) == 2
    assert science["by_region"] == {}                           # science isn't split by region

    assert digest["sections"]["sports"]["top"][0]["headline"].startswith("India beat Australia")
    assert digest["sections"]["culture"]["top"] == []           # every catalog section is present
    all_urls = [a["url"] for c in digest["sections"].values() for st in c["top"] for a in st["articles"]]
    assert not any("ft.com" in u for u in all_urls)
    assert not any(u.endswith("/old") for u in all_urls)       # older than 48h dropped
    assert next(r for r in digest["source_health"] if r["name"] == "Dead Feed")["error"].startswith("ConnectionError")
    json.dumps(digest)                                          # serializable for the app


def test_split_output_for_the_app(tmp_path):
    digest = sample_digest()
    pipeline.write_split(digest, tmp_path)
    index = json.loads((tmp_path / "index.json").read_text())
    assert "sections" not in index                              # small file: no full lists
    assert index["section_files"]["sports"] == "sections/sports.json"
    assert len(index["headlines"]["world"]) <= pipeline.HEADLINES_PER_SECTION
    assert {c["id"] for c in index["catalog"]} == set(index["section_files"])
    sports = json.loads((tmp_path / "sections" / "sports.json").read_text())
    assert sports["id"] == "sports" and sports["story_count"] == 1


def test_catalog_marks_world_with_regions():
    cat = {c["id"]: c for c in Catalog.from_file(ROOT / "sections.yaml").to_list()}
    assert [r["id"] for r in cat["world"]["regions"]][0] == "global"
    assert "regions" not in cat["sports"]
    assert {c["id"] for c in cat.values() if c["default"]} == {"world", "opinion", "business", "technology", "science"}
