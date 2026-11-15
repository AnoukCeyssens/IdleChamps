"""Account: the parts of the Player Snapshot the advisor uses, as typed domain objects."""

from __future__ import annotations

import hashlib
import json
import re
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
class ModronSave:
    """One modron core as saved: its node grid (rows of tile ids, 0 = empty) and its reset area."""

    core_id: int
    experience: int
    grid: tuple[tuple[int, ...], ...] = ()
    area_goal: int | None = None


@dataclass(frozen=True)
class GameInstance:
    """One live run (`game_instances[]`): where it is, the formation in play, and its BUD."""

    id: int
    adventure_id: int
    patron_id: int | None
    current_area: int
    highest_area: int
    formation: tuple[int, ...]  # champion id per slot, -1 empty
    bud: float | None  # `this_reset_highest_damage_dealt`
    bud_champion_id: int | None  # the champion who set it


@dataclass(frozen=True)
class Account:
    champions: Mapping[int, OwnedChampion]  # owned champions by champion id
    completed_adventure_ids: frozenset[int] = frozenset()  # free play, variants, Running Event
    best_areas: Mapping[int, int] = field(default_factory=dict)  # adventure id -> highest area
    event_tiers: Mapping[int, int] = field(default_factory=dict)  # event champion id -> tier finished
    unlocked_patron_ids: frozenset[int] = frozenset()
    running_event_ids: frozenset[int] = frozenset()
    familiar_ids: frozenset[int] = frozenset()  # familiars owned
    modron_cores: Mapping[int, int] = field(default_factory=dict)  # core id -> experience (its level)
    completed_patron_adventures: Mapping[int, object] = field(default_factory=dict)  # campaign id -> as sent
    modron_saves: Mapping[int, ModronSave] = field(default_factory=dict)  # core id -> saved setup
    server_time: int | None = None  # the snapshot's server clock (Unix seconds), `getuserdetails.current_time`
    instances: tuple[GameInstance, ...] = ()  # live runs; not part of `owned_state()`

    def owns(self, champion_id: int) -> bool:
        return champion_id in self.champions

    def owned_state(self) -> dict:
        """The recommendation-relevant subset of the account, as plain JSON-ready data: owned
        champions and levels, gear, feats, specializations, familiars, modron cores, adventure /
        variant / patron completion and `highest_area` stats. Currencies are left out on purpose."""
        return {
            "champions": {
                str(c.id): {
                    "level": c.level,
                    "specializations": sorted(c.specialization_choices),
                    "gear": {str(s): [g.rarity, g.level, g.gild] for s, g in sorted(c.gear.items())},
                    "feats": sorted(c.feats),
                    "equipped_feats": sorted(c.equipped_feats),
                    "feat_slots": c.feat_slots,
                }
                for c in sorted(self.champions.values(), key=lambda c: c.id)
            },
            "familiars": sorted(self.familiar_ids),
            "modron_cores": {str(k): v for k, v in sorted(self.modron_cores.items())},
            "completed_adventure_ids": sorted(self.completed_adventure_ids),
            "completed_patron_adventures": {str(k): v for k, v in sorted(self.completed_patron_adventures.items())},
            "unlocked_patron_ids": sorted(self.unlocked_patron_ids),
            "event_tiers": {str(k): v for k, v in sorted(self.event_tiers.items())},
            "best_areas": {str(k): v for k, v in sorted(self.best_areas.items())},
        }

    @property
    def recommendation_key(self) -> str:
        """Hash of `owned_state()`: changes only when something a recommendation depends on
        changes, so a currency-only Refresh keeps it (results are keyed on it, with the
        definitions checksum)."""
        canonical = json.dumps(self.owned_state(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    return Account(
        champions={c.id: c for c in owned},
        completed_adventure_ids=_completed_adventure_ids(raw),
        best_areas=_best_areas(details.get("stats") or {}),
        event_tiers=_event_tiers(raw.get("getcompletiondata")),
        unlocked_patron_ids=frozenset(
            int(p["patron_id"]) for p in details.get("patrons") or () if p.get("unlocked") in (True, 1, "1")
        ),
        running_event_ids=frozenset(
            int(e["event_id"]) for e in (details.get("events_details") or {}).get("active_events") or ()
        ),
        familiar_ids=frozenset(int(f["familiar_id"]) for f in details.get("familiars") or ()),
        modron_cores={
            int(core.get("core_id") or key): int(float(core.get("exp_total") or 0))
            for key, core in _entries(details.get("modron_saves"))
            if isinstance(core, dict)
        },
        modron_saves={
            save.core_id: save
            for key, core in _entries(details.get("modron_saves"))
            if isinstance(core, dict) and (save := _modron_save(key, core))
        },
        completed_patron_adventures={
            int(c["campaign_id"]): c["completed_patron_adventure_ids"]
            for c in (raw.get("getcampaigndetails") or {}).get("campaigns") or ()
            if c.get("completed_patron_adventure_ids")
        },
        server_time=int(raw["getuserdetails"]["current_time"]) if raw["getuserdetails"].get("current_time") else None,
        instances=tuple(i for i in map(_instance, details.get("game_instances") or ()) if i),
    )


def _modron_save(key, core: Mapping) -> ModronSave:
    grid = tuple(tuple(int(t or 0) for t in row) for row in core.get("grid") or () if isinstance(row, list))
    goal = str(core.get("area_goal") or "").strip()
    return ModronSave(
        core_id=int(core.get("core_id") or key),
        experience=int(float(core.get("exp_total") or 0)),
        grid=grid,
        area_goal=int(goal) if goal.isdigit() and int(goal) > 0 else None,
    )


def _instance(instance: dict) -> GameInstance | None:
    stats = instance.get("stats") or {}
    try:
        bud = float(stats.get("this_reset_highest_damage_dealt"))
    except (TypeError, ValueError):
        bud = None
    try:
        return GameInstance(
            id=int(instance["game_instance_id"]),
            adventure_id=int(instance.get("current_adventure_id") or 0),
            patron_id=int(instance.get("current_patron_id") or 0) or None,
            current_area=int(instance.get("current_area") or 0),
            highest_area=int(instance.get("highest_area") or 0),
            formation=tuple(int(c) for c in instance.get("formation") or ()),
            bud=bud if bud and bud > 0 else None,
            bud_champion_id=int(stats.get("this_reset_highest_damage_dealt_hero_id") or 0) or None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _entries(value) -> list:
    """(key, item) pairs of a value the game sends as either an object or an array."""
    if isinstance(value, dict):
        return list(value.items())
    return list(enumerate(value or ()))


_BEST_AREA = re.compile(r"highest_area_completed_ever_c(\d+)")


def _completed_adventure_ids(raw: RawResponses) -> frozenset[int]:
    """Completed adventures and variants: every campaign in `getcampaigndetails`, plus the
    Running Event's list in `getuserdetails`."""
    campaigns = (raw.get("getcampaigndetails") or {}).get("campaigns") or ()
    events = raw["getuserdetails"]["details"].get("event_details") or ()
    return frozenset(
        int(i) for source in (*campaigns, *events) for i in source.get("completed_adventure_ids") or ()
    )


def _best_areas(stats: Mapping) -> dict[int, int]:
    return {int(m.group(1)): int(v) for k, v in stats.items() if (m := _BEST_AREA.fullmatch(k)) and int(v)}


def _event_tiers(completion: dict | None) -> dict[int, int]:
    """Highest event tier each champion finished in every variant (`data.event[heroId].v[tier][variant]`,
    1 = done), read the way Emmote's event-tier tab reads it."""
    events = ((completion or {}).get("data") or {}).get("event") or {}
    tiers = {}
    for hero_id, entry in (events.items() if isinstance(events, dict) else enumerate(events)):
        by_tier = (entry or {}).get("v") if isinstance(entry, dict) else None
        if not by_tier:
            continue
        rows = by_tier.values() if isinstance(by_tier, dict) else by_tier
        rows = [list(r.values()) if isinstance(r, dict) else list(r) for r in rows]
        width = max(len(r) for r in rows)
        highest = [
            max((tier + 1 for tier, row in enumerate(rows) if variant < len(row) and row[variant] == 1), default=0)
            for variant in range(width)
        ]
        tiers[int(hero_id)] = min(highest) if highest else 0
    return tiers


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
