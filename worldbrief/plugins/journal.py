from __future__ import annotations

import html
import re

from ..plugin import register
from ..text import clean
from .rss import RSSPlugin

# What publishers put in a feed entry around the abstract.
_FRONT_MATTER = (
    re.compile(r"^.*?Author\(s\):.*?(?:<br\s*/?>|</p>|$)", re.I | re.S),       # APS, Elsevier
    re.compile(r"^.*?Published online:.*?</p>", re.I | re.S),                  # Nature
    re.compile(r"^.*?Publication date:.*?</p>", re.I | re.S),                  # Elsevier, no authors
)
_BACK_MATTER = re.compile(r"\[(?:Phys\.|Rev\.).*$|\bDOI:\s*10\..*$", re.I | re.S)
_LABEL = re.compile(r"^\W*(?:abstract|summary|background|objectives?)\W+", re.I)
_NOT_PAPERS = re.compile(
    r"^\W*(?:\[?(?:corrigendum|erratum|addendum|retraction|expression of concern)\b"
    r"|(?:author|publisher)\s+correction\b"
    r"|editorial board\b|issue information\b|(?:table of )?contents\b|masthead\b|cover (?:image|picture)\b)",
    re.I)

_SUPERSCRIPT = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
_SUBSCRIPT = str.maketrans("0123456789+-", "₀₁₂₃₄₅₆₇₈₉₊₋")
_SYMBOLS = {r"\rightarrow": "→", r"\to": "→", r"\pm": "±", r"\sim": "~", r"\times": "×"}


def _script(table: dict):
    def replace(match: re.Match) -> str:
        text = match.group(1)
        return text.translate(table) if text and all(c in "0123456789+-" for c in text) else text
    return replace


def plain_math(text: str) -> str:
    """Titles carry LaTeX and <sub> tags: ${\\mathrm{QED}}_{3}$ reads QED₃."""
    text = re.sub(r"<sub>(.*?)</sub>", r"_{\1}", text, flags=re.I)
    text = re.sub(r"<sup>(.*?)</sup>", r"^{\1}", text, flags=re.I)
    for tex, symbol in _SYMBOLS.items():
        text = text.replace(tex, symbol)
    while (unwrapped := re.sub(r"\\[a-zA-Z]+\{([^{}]*)\}", r"\1", text)) != text:
        text = unwrapped
    while (unwrapped := re.sub(r"\{\{([^{}]*)\}\}|(?<![_^])\{([^{}]*)\}", lambda m: m.group(1) or m.group(2) or "", text)) != text:
        text = unwrapped
    text = re.sub(r"\^\{([^{}]*)\}", _script(_SUPERSCRIPT), text)
    text = re.sub(r"_\{([^{}]*)\}", _script(_SUBSCRIPT), text)
    return re.sub(r"\\([a-zA-Z]+)", r"\1", text).replace("$", "")


def abstract(raw: str) -> str:
    """Just the abstract. Long author lists would otherwise fill the snippet,
    and some feeds have no abstract at all, which leaves this empty."""
    text = raw or ""
    for pattern in _FRONT_MATTER:
        text = pattern.sub("", text, count=1)
    text = plain_math(_BACK_MATTER.sub("", html.unescape(text)))
    return _LABEL.sub("", clean(text))


@register("journal")
class JournalPlugin(RSSPlugin):
    """A journal's table-of-contents feed. Papers are treated differently from news:
    `cluster` keeps each one separate and ranks it below reporting, and the paywall
    filter passes them, because an abstract page is free even when the full text
    isn't. Say which it is in the source's `note`."""

    def entry_title(self, entry) -> str:
        return clean(plain_math(html.unescape(entry.get("title") or "")), 300)

    def entry_summary(self, entry) -> str:
        return abstract(entry.get("summary") or entry.get("description") or "")

    def parse(self, content: bytes) -> list:
        journal = self.name.lower()
        return [a for a in super().parse(content)
                if not _NOT_PAPERS.search(a.title) and a.title.lower() != journal]
