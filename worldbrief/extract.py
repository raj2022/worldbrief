"""Free briefs and framing, built from feed text with no model and no API.

A brief here is 2 or 3 sentences lifted word for word from the outlets' snippets:
the ones that most resemble what other outlets said. Framing is a few headlines
from different kinds of outlet, so a reader sees how the coverage differs.

Both are pure functions of a story's articles, so a story that hasn't changed
gets the same output on every refresh.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from .cluster import STOPWORDS, keywords, similarity


def _outlet(a: dict) -> str:
    """Publisher, so two feeds of one site count once. Older stored dicts lack it."""
    return a.get("outlet") or a["source"]

MIN_OUTLETS = 2
MAX_SENTENCES = 3
MAX_CHARS = 400
MIN_SENTENCE_CHARS = 40
MAX_SENTENCE_CHARS = 300
NEAR_DUPLICATE = 0.7           # share of the shorter sentence's keywords
MIN_SHARED_TERMS = 2           # with the lead sentence, for a sentence from another outlet
MAX_FRAMING = 4
MODEL = "extractive"           # `brief.model`, which is how the app tells these from AI briefs

_BOUNDARY = re.compile(r"[.!?][\"'”’)]*\s+(?=[\"'“‘(]?[A-Z0-9])")
_INITIALS = re.compile(r"(?:[A-Za-z]\.)+")
_ABBREVIATIONS = set("""
mr. mrs. ms. dr. prof. st. sen. rep. gov. gen. lt. col. sgt. capt. cmdr. maj. rev. pres.
jr. sr. inc. ltd. co. corp. no. vs. jan. feb. mar. apr. aug. sept. sep. oct. nov. dec.
""".split())
_DATELINE = re.compile(r"^(?:[A-Z][A-Z .,'-]+)?\s*\((?:AP|AFP|Reuters)\)\s*(?:—|–|--|-)\s*")
_TRUNCATED = re.compile(r"(?:…|\.\.\.|\[…\]|\[\.\.\.\])\W*$")
_TEASER = re.compile(r"\s*\b(?:read more|continue reading|read the full|the post .+ appeared first on)\b.*$",
                     re.I)
_BOILERPLATE = re.compile(r"://|www\.|\bclick here\b|\bsign up\b|\bsubscribe\b|\bfind out more\b"
                          r"|\bhas more\b|\bmore details\b|\bwatch the\b", re.I)
# A lifted sentence opening like this points at a sentence the reader can't see.
_DANGLING = set("""
he she they it this that these those his her their its we you i but and or so also however
meanwhile instead still yet then there here such another both others
""".split())


@dataclass
class _Sentence:
    text: str
    position: int                   # within its snippet, so leads win ties
    outlets: set[str] = field(default_factory=set)
    terms: frozenset[str] = frozenset()


def split_sentences(snippet: str) -> list[str]:
    """Whole sentences only. Feed text is often cut off, so a trailing fragment
    is dropped, and a final line with no full stop is kept only if it wasn't cut."""
    text = _DATELINE.sub("", _TEASER.sub("", snippet.replace("\u200b", "")).strip())
    truncated = bool(_TRUNCATED.search(text))
    parts, start = [], 0
    for m in _BOUNDARY.finditer(text):
        last_word = text[start:m.end()].split()[-1].strip("\"'”’)(“‘")
        if last_word.lower() in _ABBREVIATIONS or _INITIALS.fullmatch(last_word):
            continue
        parts.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail and not truncated:
        if tail[-1] in ".!?\"'”’":
            parts.append(tail)
        elif tail.split()[-1].lower() not in STOPWORDS:   # "...sleep changes and" was cut too
            parts.append(tail + ".")
    return parts


def _worth_keeping(sentence: str) -> bool:
    first = sentence.split()[0].strip("\"'“‘(,").lower()
    return (MIN_SENTENCE_CHARS <= len(sentence) <= MAX_SENTENCE_CHARS
            and not sentence[0].islower()
            and first not in _DANGLING
            and not _BOILERPLATE.search(sentence))


def _candidates(articles: list[dict]) -> list[_Sentence]:
    """Each distinct sentence once, remembering every outlet that ran it. A
    sentence that only restates a headline adds nothing under that headline."""
    english = [a for a in articles if a.get("lang", "en") == "en"]
    headlines = [keywords(a["title"]) for a in english]
    found: dict[frozenset[str], _Sentence] = {}
    for a in english:
        for position, text in enumerate(split_sentences(a.get("summary") or "")):
            terms = keywords(text)
            if not terms or not _worth_keeping(text):
                continue
            if any(similarity(terms, h) >= 0.9 and len(terms) <= len(h) + 1 for h in headlines):
                continue
            found.setdefault(terms, _Sentence(text, position, terms=terms)).outlets.add(_outlet(a))
    return list(found.values())


def _centrality(sentences: list[_Sentence]) -> list[float]:
    """TF-IDF cosine similarity to what *other* outlets wrote. Words are counted
    once per sentence, so tf is 0 or 1 and a sentence's weight is just its idf."""
    n = len(sentences)
    df: dict[str, int] = {}
    for s in sentences:
        for w in s.terms:
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log(1 + n / d) for w, d in df.items()}
    norms = [math.sqrt(sum(idf[w] ** 2 for w in s.terms)) for s in sentences]

    def score(i: int) -> float:
        s = sentences[i]
        repeated = len(s.outlets) - 1          # running a sentence verbatim is agreement too
        return repeated + sum(
            sum(idf[w] ** 2 for w in s.terms & other.terms) / (norms[i] * norms[j])
            for j, other in enumerate(sentences)
            if j != i and other.outlets - s.outlets)

    return [score(i) for i in range(n)]


def extract_brief(articles: list[dict]) -> str:
    """The most central sentences, or "" if the snippets give nothing usable."""
    sentences = _candidates(articles)
    if not sentences:
        return ""
    scores = _centrality(sentences)
    ranked = sorted(zip(scores, sentences), key=lambda p: (-p[0], p[1].position, p[1].text))
    chosen: list[_Sentence] = []
    length = 0
    for _, s in ranked:
        extra = len(s.text) + (1 if chosen else 0)
        if length + extra > MAX_CHARS:
            continue
        if any(similarity(s.terms, c.terms) >= NEAR_DUPLICATE for c in chosen):
            continue
        # Clustering matches on headlines, so "October worst on record" can join
        # Russian losses to hospital waits. Don't splice two events into one brief.
        if chosen and not (s.outlets & chosen[0].outlets) \
                and len(s.terms & chosen[0].terms) < MIN_SHARED_TERMS:
            continue
        chosen.append(s)
        length += extra
        if len(chosen) == MAX_SENTENCES:
            break
    return " ".join(s.text for s in chosen)


def pick_framing(articles: list[dict]) -> list[dict]:
    """Up to MAX_FRAMING headlines, one per outlet (newest first, as the story
    lists them). Each pick favours whatever the earlier picks haven't shown yet."""
    pool = list({_outlet(a): a for a in reversed(articles)}.values())[::-1]
    picked: list[dict] = []
    domains: set[str] = set()
    regions: set[str] = set()
    owners: set[str] = set()
    wordings: set[frozenset[str]] = set()

    def gain(a: dict) -> int:
        # Two feeds of one publisher aren't two outlets, so that outranks the rest.
        return (4 * (a.get("domain") not in domains)
                + 2 * (a.get("region") not in regions)
                + 2 * (a.get("ownership") not in owners)
                + (keywords(a["title"]) not in wordings))

    while pool and len(picked) < MAX_FRAMING:
        a = max(pool, key=gain)         # max keeps the first on a tie: the newest
        pool.remove(a)
        picked.append(a)
        domains.add(a.get("domain"))
        regions.add(a.get("region"))
        owners.add(a.get("ownership"))
        wordings.add(keywords(a["title"]))
    return [{"source": a["source"], "ownership": a.get("ownership") or "unknown",
             "region": a.get("region", ""), "headline": a["title"], "url": a["url"]}
            for a in picked]


def add_free_coverage(story: dict, now: datetime) -> None:
    """Set `framing` and `brief` on a multi-outlet story. `brief` has the same
    shape as an AI one, so an installed app can still read the feed."""
    if story["source_count"] < MIN_OUTLETS:
        return
    story["framing"] = pick_framing(story["articles"])
    summary = extract_brief(story["articles"])
    if summary:
        story["brief"] = {
            "summary": summary,
            "why_it_matters": "",
            "source_count": len({_outlet(a) for a in story["articles"] if a.get("summary")}),
            "generated_at": story["latest"] or now.isoformat(),
            "model": MODEL,
        }
