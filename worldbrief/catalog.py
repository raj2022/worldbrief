"""The section catalog: what users can pick in the app."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "sections.yaml"

REGIONS = [
    {"id": "global", "title": "International"},
    {"id": "africa", "title": "Africa"},
    {"id": "asia", "title": "Asia"},
    {"id": "middle_east", "title": "Middle East"},
    {"id": "europe", "title": "Europe"},
    {"id": "americas", "title": "Americas"},
    {"id": "oceania", "title": "Oceania"},
]
REGION_IDS = {r["id"] for r in REGIONS}


@dataclass
class Section:
    id: str
    title: str
    symbol: str
    description: str = ""
    default: bool = False
    by_region: bool = False
    subtopics: list[dict] = field(default_factory=list)
    # How long an item stays in the section. News goes stale fast; opinion
    # and essays are read at leisure, so they stay for a week.
    max_age_hours: float = 36

    @property
    def subtopic_ids(self) -> set[str]:
        return {t["id"] for t in self.subtopics}

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.by_region:
            d["regions"] = REGIONS
        return d


class Catalog:
    def __init__(self, sections: list[Section]):
        ids = [s.id for s in sections]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate section id in catalog")
        self.sections = sections
        self.by_id = {s.id: s for s in sections}

    @classmethod
    def from_file(cls, path: Path = DEFAULT_FILE) -> "Catalog":
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls([Section(**s) for s in data.get("sections", [])])

    def check_source(self, name: str, category: str, topic: str, region: str) -> list[str]:
        """Return problems with a source's section/topic/region, if any."""
        problems = []
        sec = self.by_id.get(category)
        if sec is None:
            return [f"{name}: unknown section {category!r}"]
        if topic and topic not in sec.subtopic_ids:
            problems.append(f"{name}: section {category!r} has no subtopic {topic!r}")
        if sec.subtopics and not topic:
            problems.append(f"{name}: section {category!r} needs a topic ({sorted(sec.subtopic_ids)})")
        if region not in REGION_IDS:
            problems.append(f"{name}: unknown region {region!r}")
        return problems

    def to_list(self) -> list[dict]:
        return [s.to_dict() for s in self.sections]
