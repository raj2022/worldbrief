"""Free briefs and framing: no key, no model, no network."""
import builtins
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from worldbrief import __main__ as cli
from worldbrief.catalog import Catalog
from worldbrief.extract import MAX_CHARS, MAX_FRAMING, extract_brief, pick_framing, split_sentences
from worldbrief.models import Article
from worldbrief.paywall import PaywallFilter
from worldbrief.pipeline import build_digest, write_morning, write_split

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
TITLE = "Geneva ceasefire talks resume between leaders"
A = "Negotiators from both sides returned to Geneva on Sunday for ceasefire talks."
B = "Mediators said ceasefire talks in Geneva would focus on opening aid routes."
C = "Aid agencies warned that convoys cannot reach the border without a ceasefire."


def art(source, summary="", title=TITLE, category="world", region="global", ownership="independent",
        domain=None, hours_ago=1.0, lang="en"):
    host = domain or f"{source.lower().replace(' ', '')}.example"
    return Article(title=title, url=f"https://{host}/{abs(hash(title)) % 999}", source=source,
                   category=category, region=region, ownership=ownership, summary=summary,
                   lang=lang, published=NOW - timedelta(hours=hours_ago))


def dicts(*articles):
    return [a.to_dict() for a in articles]


def digest_of(articles):
    return build_digest(articles, [], PaywallFilter.from_file(), Catalog.from_file(), now=NOW)


def top(digest, section="world"):
    return digest["sections"][section]["top"][0]


@pytest.mark.parametrize("text, expected", [
    ("Dr. Lee spoke to the U.S. Senate today. Nobody answered.",
     ["Dr. Lee spoke to the U.S. Senate today.", "Nobody answered."]),
    ("One whole sentence here. The majority of those…", ["One whole sentence here."]),
    ("One whole sentence here. Another cut off...", ["One whole sentence here."]),
    ("Workers kept the balance of payments in surplus", ["Workers kept the balance of payments in surplus."]),
    ("Doctors updated guidance on sleep changes and", []),
    ("Support is rising in Kaduna Read More: https://punch.example/x", ["Support is rising in Kaduna."]),
    ("WASHINGTON (AP) — Lawyers filed the suit.", ["Lawyers filed the suit."]),
    ('He said "stop." Then he left.', ['He said "stop."', "Then he left."]),
])
def test_split_sentences(text, expected):
    assert split_sentences(text) == expected


def test_picks_what_outlets_agree_on_over_the_outlier():
    arts = dicts(art("BBC", A), art("DW", B), art("NPR", C),
                 art("Sky", "Separately, a football club in Leeds announced a new manager on Sunday."))
    brief = extract_brief(arts)
    assert A in brief and B in brief and "Leeds" not in brief


def test_no_duplicates_across_outlets_even_when_reworded():
    arts = dicts(art("BBC", A), art("DW", A), art("NPR", "Negotiators from both sides returned to Geneva for ceasefire talks on Sunday."))
    assert extract_brief(arts).count("Negotiators") == 1


def test_never_more_than_three_sentences_or_the_character_limit():
    long = [f"Ceasefire talks in Geneva heard point number {w} about {w} aid routes and prisoner swaps, "
            f"with mediators from Qatar and Norway saying that {w} remained open for discussion today."
            for w in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")]
    brief = extract_brief(dicts(*[art(f"O{i}", s) for i, s in enumerate(long)]))
    assert 0 < len(brief) < MAX_CHARS and len(split_sentences(brief)) <= 3


def test_unrelated_events_are_not_spliced_together():
    """The headline overlap put these two in one story, on a real day's feeds."""
    arts = dicts(art("Kyiv", "Estimated Russian troop losses reached 54,400 soldiers in October, the General Staff said."),
                 art("RTE", "October was the worst on record for hospital overcrowding, the nurses organisation said."))
    assert len(split_sentences(extract_brief(arts))) == 1


def test_restating_the_headline_or_pointing_elsewhere_is_not_a_brief():
    arts = dicts(art("BBC", TITLE + "."), art("DW", "He said it would continue through the week in Geneva."),
                 art("NPR", "Read the full story at https://npr.example/ceasefire now"), art("Sky", ""))
    assert extract_brief(arts) == ""


def test_other_languages_stay_out_of_an_english_brief():
    arts = dicts(art("BBC", A), art("Agência", "Negociadores voltaram a Genebra no domingo para discutir o cessar-fogo.", lang="pt"))
    assert extract_brief(arts) == A


def test_same_input_same_output():
    arts = dicts(art("BBC", A), art("DW", B), art("NPR", C))
    assert extract_brief(arts) == extract_brief(list(reversed(arts)))


def test_framing_spreads_across_regions_and_ownership():
    arts = dicts(art("A1", title="Talks resume", region="europe", hours_ago=1),
                 art("A2", title="Talks restart", region="europe", hours_ago=2),
                 art("A3", title="Talks begin", region="europe", hours_ago=3),
                 art("Bangkok", title="Leaders meet", region="asia", hours_ago=4),
                 art("TASS", title="West pressured", region="europe", ownership="state_linked", hours_ago=5),
                 art("NPR", title="Hope for truce", region="americas", ownership="public", hours_ago=6))
    picked = pick_framing(arts)
    assert len(picked) == MAX_FRAMING
    assert {p["source"] for p in picked} >= {"Bangkok", "TASS", "NPR"}
    assert set(picked[0]) == {"source", "ownership", "region", "headline", "url"}


def test_framing_uses_one_headline_per_outlet_and_prefers_new_wording():
    arts = dicts(art("BBC", title="Newest take"), art("BBC", title="Older take", hours_ago=5),
                 art("DW", title="Older take", hours_ago=2), art("NPR", title="Third take", hours_ago=3))
    picked = pick_framing(arts)
    assert [p["source"] for p in picked].count("BBC") == 1
    assert next(p for p in picked if p["source"] == "BBC")["headline"] == "Newest take"


def test_two_feeds_of_one_publisher_come_after_other_outlets():
    arts = dicts(*[art(f"BBC {i}", domain="bbc.example", title=f"Take {i}") for i in range(5)],
                 art("DW", title="Other take", hours_ago=9))
    assert "DW" in [p["source"] for p in pick_framing(arts)]


def test_multi_outlet_story_gets_brief_and_framing_and_opinion_too():
    d = digest_of([art("BBC", A), art("DW", B), art("Times", A, category="opinion"), art("Guardian", B, category="opinion")])
    st = top(d)
    assert st["brief"]["model"] == "extractive" and st["brief"]["why_it_matters"] == ""
    assert st["brief"]["source_count"] == 2 and st["brief"]["generated_at"] == st["latest"]
    assert len(st["framing"]) == 2
    assert top(d, "opinion")["brief"] and d["stats"]["extractive_briefs"] == 2


def test_single_outlet_story_gets_neither():
    st = top(digest_of([art("BBC", A)]))
    assert "brief" not in st and "framing" not in st


def test_capped_section_files_keep_all_framing(tmp_path):
    arts = [art(f"O{i}", A, region=r) for i, r in enumerate(["asia", "africa", "europe", "americas"] * 2)]
    d = digest_of(arts)
    write_split(d, tmp_path)
    st = json.loads((tmp_path / "sections" / "world.json").read_text())["top"][0]
    assert len(st["articles"]) == 5 and len(st["framing"]) == 4


def test_morning_comes_from_free_briefs_and_skips_opinion(tmp_path):
    arts = []
    for i, (title, topic) in enumerate([("Harbor strike halts container shipping", "harbor strike"),
                                        ("Budget vote splits ruling coalition", "budget vote"),
                                        ("Vaccine shortage hits rural clinics", "vaccine shortage"),
                                        ("Rocket reaches orbit after delays", "rocket launch")]):
        arts += [art(f"A{i}", f"Officials said the {topic} dominated the week and more was expected on Sunday.", title),
                 art(f"B{i}", f"Reporters covering the {topic} said officials expected more of it on Monday.", title)]
    arts += [art("Times", A, category="opinion"), art("Guardian", B, category="opinion")]
    d = digest_of(arts)
    assert write_morning(d, tmp_path, tmp_path, (11,), NOW)
    stories = json.loads((tmp_path / "morning.json").read_text())["stories"]
    assert len(stories) == 4 and all(st["category"] == "world" for st in stories)


def test_no_key_means_no_briefer_and_no_warning(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert cli._briefer(Path(".")) is None
    assert not caplog.records


def test_key_without_the_package_falls_back_with_one_warning(monkeypatch, caplog):
    real = builtins.__import__
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delitem(sys.modules, "worldbrief.briefs", raising=False)

    def fake(name, *a, **kw):
        if name == "anthropic":
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert cli._briefer(Path(".")) is None
    assert len(caplog.records) == 1 and "requirements-ai" in caplog.text
