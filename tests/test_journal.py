"""Journal feeds: readable titles and abstracts, and papers kept apart from news."""
from datetime import datetime, timezone

import pytest

from worldbrief import plugin
from worldbrief.cluster import cluster
from worldbrief.models import Article
from worldbrief.paywall import PaywallFilter
from worldbrief.pipeline import TOPIC_SOURCE_SHARE, spread_sources
from worldbrief.plugins.journal import abstract, plain_math

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def feed(*items):
    body = "".join(f"<item><title>{t}</title><link>https://www.sciencedirect.com/{i}</link>"
                   f"<description>{d}</description></item>" for i, (t, d) in enumerate(items))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>J</title>{body}</channel></rss>'.encode()


def journal():
    return plugin.create({"type": "journal", "name": "Physics Letters B", "url": "https://x", "category": "science"})


def paper(title, url, source, plugin_name="journal"):
    return Article(title=title, url=url, source=source, category="science", plugin=plugin_name, published=NOW)


@pytest.mark.parametrize("tex, expected", [
    ("Emergent Gauge Flux in Mixed ${\\mathrm{QED}}_{3}$ with Flavor", "Emergent Gauge Flux in Mixed QED₃ with Flavor"),
    ("Prompt neutrons in fission of $^{258}$Fm", "Prompt neutrons in fission of ²⁵⁸Fm"),
    ("Effects in $b→sℓℓ$ decays", "Effects in b→sℓℓ decays"),
    ("Mixing of <em>p<sub>T</sub></em> and Rh<sup>3+</sup>", "Mixing of <em>pT</em> and Rh³⁺"),
])
def test_plain_math(tex, expected):
    assert plain_math(tex) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Author(s): A. One, B. Two<br/>We measure the mass.<br/>[Phys. Rev. D 113, 1] Published 1 Nov", "We measure the mass."),
    ("<p>Publication date: December 2026</p><p><b>Source:</b> Physics Letters B, Volume 883</p><p>Author(s): A. One</p>", ""),
    ("<p>Nature Physics, Published online: 30 October 2026; <a href='x'>doi:1</a></p>We observe a phase.", "We observe a phase."),
    ('<div class="boxTitle">Abstract</div>Quarks are studied here.', "Quarks are studied here."),
])
def test_abstract_drops_publisher_boilerplate(raw, expected):
    assert abstract(raw) == expected


def test_corrections_and_the_journal_itself_are_not_papers():
    p = journal()
    got = p.parse(feed(("Corrigendum to “A result”", "x"), ("Author Correction: Rh", "x"), ("Editorial Board", "x"),
                       ("[Erratum] A thing", "x"), ("Physics Letters B", "x"), ("Search for dark photons", "Author(s): A<br/>We search.")))
    assert [a.title for a in got] == ["Search for dark photons"]
    assert got[0].plugin == "journal" and got[0].summary == "We search."


def test_journals_pass_the_paywall_filter_but_their_publishers_stay_blocked():
    pw = PaywallFilter.from_file()
    url = "https://www.nature.com/articles/s1"
    assert pw.is_free(paper("T", url, "Nature Physics"))
    assert not pw.is_free(paper("T", url, "Hacker News", "hackernews"))


def test_papers_never_cluster_with_each_other_or_news():
    title = "Search for dark photons at the Large Hadron Collider with ATLAS"
    stories = cluster([paper(title, "https://a/1", "PRD"), paper(title, "https://b/1", "JHEP"),
                       paper(title, "https://c/1", "CERN Courier", "rss")])
    assert len(stories) == 3


def test_news_outranks_an_equally_fresh_paper():
    news, study = cluster([paper("Higgs mass news today", "https://a/1", "A", "rss"),
                           paper("Measuring something else entirely", "https://b/1", "B")])
    assert news.score(NOW) > study.score(NOW)


def listed(*sources):
    return [{"articles": [{"source": s}]} for s in sources]


def test_one_source_cannot_fill_a_topic_while_others_wait():
    stories = listed(*["PLB"] * 30, *["PRD"] * 3, "JHEP")
    picked = [st["articles"][0]["source"] for st in spread_sources(stories, 10)]
    assert picked.count("PLB") == TOPIC_SOURCE_SHARE + 1 and "PRD" in picked and "JHEP" in picked


def test_a_low_ranked_weekly_journal_still_gets_a_turn():
    stories = listed(*[f"Daily {i}" for i in range(6) for _ in range(9)], "JHEP", "JHEP")
    picked = [st["articles"][0]["source"] for st in spread_sources(stories, 14)]
    assert picked.count("JHEP") == 2


def test_a_topic_with_one_source_keeps_its_full_list_in_order():
    stories = listed(*["Kabaddi Adda"] * 30)
    assert spread_sources(stories, 12) == stories[:12]
