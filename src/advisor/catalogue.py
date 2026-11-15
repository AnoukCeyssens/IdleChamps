"""Catalogue: the parts of the Game Definitions the advisor uses, as typed domain objects."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

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
class SpecializationOption:
    id: int  # the upgrade id the Player Snapshot records when it is chosen
    name: str
    description: str


@dataclass(frozen=True)
class SpecializationChoice:
    """One point where the game asks the player to pick a specialization."""

    required_level: int
    options: tuple[SpecializationOption, ...]  # in upgrade id order


GEAR_SLOTS = (1, 2, 3, 4, 5, 6)


@dataclass(frozen=True)
class GearItem:
    """The game's item for one champion's gear slot at one rarity."""

    champion_id: int
    slot: int
    rarity: int  # 1 Common, 2 Uncommon, 3 Rare, 4 Epic
    name: str
    description: str


@dataclass(frozen=True)
class Feat:
    id: int
    champion_id: int
    name: str
    description: str


@dataclass(frozen=True)
class Catalogue:
    champions: tuple[Champion, ...]  # ordered by seat, then id
    # The champion's specialization choice points by champion id, ordered by required level.
    # Only champions whose upgrade definitions have been loaded appear.
    specializations: Mapping[int, tuple[SpecializationChoice, ...]] = field(default_factory=dict)
    # Gear items by (champion id, slot, rarity); only those whose loot definitions have been loaded.
    gear: Mapping[tuple[int, int, int], GearItem] = field(default_factory=dict)

    # Feats by id; only those whose feat definitions have been loaded.
    feats: Mapping[int, Feat] = field(default_factory=dict)

    def gear_item(self, champion_id: int, slot: int, rarity: int) -> GearItem | None:
        return self.gear.get((champion_id, slot, rarity))

    def feats_of(self, champion_id: int) -> tuple[Feat, ...]:
        return tuple(sorted((f for f in self.feats.values() if f.champion_id == champion_id), key=lambda f: f.id))

    def champion(self, champion_id: int) -> Champion:
        return next(c for c in self.champions if c.id == champion_id)

    def find(self, champion_id: int) -> Champion | None:
        return next((c for c in self.champions if c.id == champion_id), None)


def parse_catalogue(raw: RawResponses) -> Catalogue:
    champions = [
        _champion(h)
        for h in raw["getdefinitions"]["hero_defines"]
        if h.get("seat_id")  # seat 0 marks the game's "Please Reload" placeholders
    ]
    return Catalogue(
        champions=tuple(sorted(champions, key=lambda c: (c.seat, c.id))),
        specializations=_specializations(_table(raw, "upgrade_defines")),
        gear=_gear(_table(raw, "loot_defines")),
        feats=_feats(_table(raw, "hero_feat_defines")),
    )


def _feats(rows: list[dict]) -> dict[int, Feat]:
    return {
        int(row["id"]): Feat(
            id=int(row["id"]),
            champion_id=int(row["hero_id"]),
            name=row.get("name") or "",
            description=row.get("description") or "",
        )
        for row in rows
        if row.get("hero_id")
    }


def _gear(loot: list[dict]) -> dict[tuple[int, int, int], GearItem]:
    items = (
        GearItem(
            champion_id=int(row["hero_id"]),
            slot=int(row["slot_id"]),
            rarity=int(row["rarity"]),
            name=row.get("name") or "",
            description=row.get("description") or "",
        )
        for row in loot
        if row.get("hero_id") and row.get("slot_id") and row.get("rarity")
    )
    return {(i.champion_id, i.slot, i.rarity): i for i in items}


def _table(raw: RawResponses, name: str) -> list[dict]:
    """Every row of one Game Definitions table, wherever the loaded responses carry it.

    A table can come whole (`upgrade_defines`) or sharded (`upgrade_defines_0`, `_1`, ...) in
    `getdefinitions`, and a subset rides along in `getuserdetails.defines`. Rows are merged by
    id; `getdefinitions` wins over the embedded subset.
    """
    shard = re.compile(rf"{re.escape(name)}(_\d+)?")
    sources = [
        (raw.get("getuserdetails") or {}).get("defines") or {},
        raw.get("getdefinitions") or {},
    ]
    merged: dict = {}
    for source in sources:
        for key, rows in source.items():
            if shard.fullmatch(key) and isinstance(rows, list):
                merged.update({row["id"]: row for row in rows if isinstance(row, dict) and "id" in row})
    return list(merged.values())


def _specializations(upgrades: list[dict]) -> dict[int, tuple[SpecializationChoice, ...]]:
    points: dict[tuple[int, int, int], list[SpecializationOption]] = defaultdict(list)
    for u in sorted(upgrades, key=lambda u: int(u["id"])):
        if not u.get("specialization_name"):
            continue
        key = (int(u["hero_id"]), int(u.get("required_level") or 0), int(u.get("required_upgrade_id") or 0))
        points[key].append(
            SpecializationOption(
                id=int(u["id"]),
                name=u["specialization_name"],
                description=u.get("specialization_description") or "",
            )
        )
    by_champion: dict[int, list[SpecializationChoice]] = defaultdict(list)
    for (hero_id, level, _), options in sorted(points.items()):
        by_champion[hero_id].append(SpecializationChoice(required_level=level, options=tuple(options)))
    return {hero_id: tuple(choices) for hero_id, choices in by_champion.items()}


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
