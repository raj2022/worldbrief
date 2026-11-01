import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SPACE_PUNCT = re.compile(r"\s+([.,;:!?])")


def clean(text: str | None, limit: int = 400) -> str:
    """Strip HTML, collapse whitespace, cap length. We only ever keep a short
    snippet — the app links out to the publisher for the full article."""
    if not text:
        return ""
    t = _WS.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()
    t = _SPACE_PUNCT.sub(r"\1", t)
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t
