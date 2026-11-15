"""Account: the parts of the Player Snapshot the advisor uses, as typed domain objects."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from advisor.request_log import RawResponses


RARITY_NAMES = {1: "Common", 2: "Uncommon", 3: "Rare", 4: "Epic"}
GILD_NAMES = {1: "Shiny", 2: "Golden"}


@dataclass(frozen=True)
class OwnedGear:
    """The item a champion holds in one gear slot."""

    slot: int
    rarity: int  # 1 Common, 2 Uncommon, 3 Rare, 4 Epic
    level: int  # item level; the snapshot's `enchant` is level - 1 (community convention)
    gild: int = 0  # 0 none, 1 Shiny, 2 Golden

    @property
    def rarity_name(self) -> str:
        return RARITY_NAMES.get(self.rarity, f"Rarity {self.rarity}")

    @property
    def gild_name(self) -> str | None:
        return GILD_NAMES.get(self.gild)


@dataclass(frozen=True)
class OwnedChampion:
    id: int
    level: int
    specialization_choices: frozenset[int] = frozenset()  # upgrade ids of the chosen specializations
    gear: Mapping[int, OwnedGear] = field(default_factory=dict)  # by slot; empty slots absent
    feats: tuple[int, ...] = ()  # feat ids owned (unlocked in the game's terms)
    equipped_feats: frozenset[int] = frozenset()  # feat ids active in the champion's feat slots
    feat_slots: int = 0


@dataclass(frozen=True)
class Account:
    champions: Mapping[int, OwnedChampion]  # owned champions by champion id

    def owns(self, champion_id: int) -> bool:
        return champion_id in self.champions


def parse_account(raw: RawResponses) -> Account:
    details = raw["getuserdetails"]["details"]
    gear: dict[int, dict[int, OwnedGear]] = defaultdict(dict)
    for item in details.get("loot") or ():
        owned_gear = OwnedGear(
            slot=int(item["slot_id"]),
            rarity=int(item["rarity"]),
            level=int(item.get("enchant") or 0) + 1,
            gild=int(item.get("gild") or 0),
        )
        gear[int(item["hero_id"])][owned_gear.slot] = owned_gear
    owned = (_owned_champion(h, gear) for h in details["heroes"] if str(h.get("owned")) == "1")
    return Account(champions={c.id: c for c in owned})


def _owned_champion(hero: dict, gear: Mapping[int, Mapping[int, OwnedGear]]) -> OwnedChampion:
    champion_id = int(hero["hero_id"])  # numbers arrive as strings
    return OwnedChampion(
        id=champion_id,
        level=int(hero["level"]),
        specialization_choices=frozenset(int(u) for u in hero.get("specialization_choices") or ()),
        gear=dict(gear.get(champion_id, {})),
        feats=tuple(int(f) for f in hero.get("unlocked_feats") or ()),
        equipped_feats=frozenset(int(f) for f in hero.get("active_feats") or ()),
        feat_slots=int(hero.get("feat_slots") or 0),
    )
