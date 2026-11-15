"""Catalogue: the parts of the Game Definitions the advisor uses, as typed domain objects."""

from __future__ import annotations

import re
from dataclasses import dataclass

from advisor.request_log import RawResponses

# The champion tags that describe a role, in the order the filter offers them.
ROLE_TAGS = ("dps", "support", "healing", "tanking", "gold", "speed", "control", "debuff", "breaking")

_YEAR_TAG = re.compile(r"y(\d+)")


@dataclass(frozen=True)
class ChampionSource:
    """Where a champion comes from: an event (with its year), the core set, or chests."""

    kind: str  # "event" | "core" | "chest"
    event_name: str | None = None
    year: int | None = None

    @property
    def label(self) -> str:
        if self.kind == "event":
            return f"{self.event_name}, Year {self.year}" if self.year else f"{self.event_name}"
        return "Core" if self.kind == "core" else "Chest"


@dataclass(frozen=True)
class Champion:
    id: int
    name: str
    seat: int
    tags: frozenset[str]
    source: ChampionSource

    @property
    def role_tags(self) -> tuple[str, ...]:
        return tuple(t for t in ROLE_TAGS if t in self.tags)


@dataclass(frozen=True)
class Catalogue:
    champions: tuple[Champion, ...]  # ordered by seat, then id

    def champion(self, champion_id: int) -> Champion:
        return next(c for c in self.champions if c.id == champion_id)


def parse_catalogue(raw: RawResponses) -> Catalogue:
    champions = [
        _champion(h)
        for h in raw["getdefinitions"]["hero_defines"]
        if h.get("seat_id")  # seat 0 marks the game's "Please Reload" placeholders
    ]
    return Catalogue(champions=tuple(sorted(champions, key=lambda c: (c.seat, c.id))))


def _champion(hero: dict) -> Champion:
    tags = frozenset(hero.get("tags") or ())
    return Champion(
        id=int(hero["id"]),
        name=hero["name"],
        seat=int(hero["seat_id"]),
        tags=tags,
        source=_source(hero, tags),
    )


def _source(hero: dict, tags: frozenset[str]) -> ChampionSource:
    if hero.get("event_name"):
        years = [int(m.group(1)) for t in tags if (m := _YEAR_TAG.fullmatch(t))]
        return ChampionSource("event", hero["event_name"], min(years) if years else None)
    return ChampionSource("core" if "core" in tags else "chest")
