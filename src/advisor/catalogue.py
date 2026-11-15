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
    base_damage: float = 0.0
    ability_scores: Mapping[str, int] = field(default_factory=dict, hash=False)  # "str", "dex", ... (character sheet)
    age: int | None = None
    date_available: str = ""  # "YYYY-MM-DD hh:mm:ss"
    last_rework_date: str = ""

    @property
    def role_tags(self) -> tuple[str, ...]:
        return tuple(t for t in ROLE_TAGS if t in self.tags)


@dataclass(frozen=True)
class Campaign:
    """A free play campaign, or an event (an event's adventures form a campaign of their own)."""

    id: int
    name: str
    event_id: int | None = None  # set on event campaigns
    favor_name: str | None = None  # the reset currency (Favor) earned here, e.g. "Torm's Favor"

    @property
    def is_event(self) -> bool:
        return self.event_id is not None


@dataclass(frozen=True)
class Slot:
    """One seat of a formation layout. Column 0 is the front (nearest the enemies)."""

    index: int
    x: int
    y: int
    column: int
    adjacent: tuple[int, ...]


@dataclass(frozen=True)
class Restriction:
    """One machine-readable rule on which champions may be used (`only_allow_crusaders` or
    `disallow_crusaders`). A champion matches when any of its criteria hold: listed by id, carries
    one of the tags, meets the stat checks (all of them, or any with `stats_any`), or satisfies
    `expr`. Evaluated by `advisor.restrictions`."""

    kind: str  # "only_allow" | "disallow"
    ids: frozenset[int] = frozenset()
    tags: tuple[str, ...] = ()
    stats: tuple[tuple[str, str, int], ...] = ()  # (stat, comparison, value), e.g. ("con", ">=", 14)
    stats_any: bool = False
    expr: str | None = None


@dataclass(frozen=True)
class ForcedChampion:
    """A champion the adventure puts in the formation (`force_use_heroes`), optionally in a fixed slot."""

    champion_id: int
    slot: int | None = None
    start_level: int | None = None


@dataclass(frozen=True)
class Adventure:
    id: int
    name: str
    campaign_id: int
    base_id: int | None  # the base adventure of a variant (`variant_adventure_id`)
    objective_area: int | None  # None for free play
    restrictions_text: str
    patron_objectives: Mapping[int, int]  # patron id -> objective area
    layout: tuple[Slot, ...]
    blocked_slots: frozenset[int]  # seats taken by an escort
    champion_id: int | None = None  # the event champion whose adventure this is, if any
    restrictions: tuple[Restriction, ...] = ()
    forced: tuple[ForcedChampion, ...] = ()

    @property
    def is_variant(self) -> bool:
        return self.base_id is not None


@dataclass(frozen=True)
class Patron:
    id: int
    name: str
    restrictions: tuple[Restriction, ...] = ()


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
    effects: tuple[str, ...] = ()  # effect strings at this rarity, e.g. "global_dps_multiplier_mult,10"


@dataclass(frozen=True)
class Feat:
    id: int
    champion_id: int
    name: str
    description: str
    effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Upgrade:
    """A level-gated champion upgrade. `effect` is the game's compact effect string: a key with its
    parameters (`hero_dps_multiplier_mult,400`) or a link to an ability (`effect_def,568`)."""

    id: int
    champion_id: int
    required_level: int
    effect: str
    name: str = ""
    is_specialization: bool = False


@dataclass(frozen=True)
class EffectDefinition:
    """An ability from `effect_defines`: its effect keys as published (each an `effect_string` plus
    optional fields such as `targets`), and its description."""

    id: int
    effect_keys: tuple[Mapping, ...]
    description: str = ""


@dataclass(frozen=True)
class Catalogue:
    champions: tuple[Champion, ...]  # ordered by seat, then id
    campaigns: tuple[Campaign, ...] = ()
    adventures: tuple[Adventure, ...] = ()  # ordered by id
    patrons: Mapping[int, Patron] = field(default_factory=dict)
    # The champion's specialization choice points by champion id, ordered by required level.
    # Only champions whose upgrade definitions have been loaded appear.
    specializations: Mapping[int, tuple[SpecializationChoice, ...]] = field(default_factory=dict)
    # Gear items by (champion id, slot, rarity); only those whose loot definitions have been loaded.
    gear: Mapping[tuple[int, int, int], GearItem] = field(default_factory=dict)

    # Feats by id; only those whose feat definitions have been loaded.
    feats: Mapping[int, Feat] = field(default_factory=dict)
    # Upgrades and abilities by id; only those whose definitions have been loaded.
    upgrades: Mapping[int, Upgrade] = field(default_factory=dict)
    effects: Mapping[int, EffectDefinition] = field(default_factory=dict)

    def upgrades_of(self, champion_id: int) -> tuple[Upgrade, ...]:
        return tuple(sorted((u for u in self.upgrades.values() if u.champion_id == champion_id), key=lambda u: u.id))

    def gear_item(self, champion_id: int, slot: int, rarity: int) -> GearItem | None:
        return self.gear.get((champion_id, slot, rarity))

    def feats_of(self, champion_id: int) -> tuple[Feat, ...]:
        return tuple(sorted((f for f in self.feats.values() if f.champion_id == champion_id), key=lambda f: f.id))

    def champion(self, champion_id: int) -> Champion:
        return next(c for c in self.champions if c.id == champion_id)

    def adventure(self, adventure_id: int) -> Adventure | None:
        return next((a for a in self.adventures if a.id == adventure_id), None)

    def campaign(self, campaign_id: int) -> Campaign:
        found = next((c for c in self.campaigns if c.id == campaign_id), None)
        return found or Campaign(campaign_id, f"Campaign {campaign_id}")

    def variants_of(self, adventure_id: int) -> tuple[Adventure, ...]:
        return tuple(a for a in self.adventures if a.base_id == adventure_id)

    def patron(self, patron_id: int) -> Patron:
        return self.patrons.get(patron_id) or Patron(patron_id, f"Patron {patron_id}")
    def find(self, champion_id: int) -> Champion | None:
        return next((c for c in self.champions if c.id == champion_id), None)


def parse_catalogue(raw: RawResponses) -> Catalogue:
    champions = [
        _champion(h)
        for h in raw["getdefinitions"]["hero_defines"]
        if h.get("seat_id")  # seat 0 marks the game's "Please Reload" placeholders
    ]
    campaign_defines = _table(raw, "campaign_defines")
    adventure_defines = {int(a["id"]): a for a in _table(raw, "adventure_defines")}
    campaign_layouts = {int(c["id"]): _layout(c.get("game_changes")) for c in campaign_defines}
    champion_of = {
        int(ref["adventure_id"]): int(h["id"])
        for h in raw["getdefinitions"]["hero_defines"]
        for ref in h.get("adventure_ids") or ()
        if h.get("event_name")
    }
    return Catalogue(
        champions=tuple(sorted(champions, key=lambda c: (c.seat, c.id))),
        specializations=_specializations(_table(raw, "upgrade_defines")),
        gear=_gear(_table(raw, "loot_defines")),
        feats=_feats(_table(raw, "hero_feat_defines")),
        upgrades=_upgrades(_table(raw, "upgrade_defines")),
        effects=_effects(_table(raw, "effect_defines")),
        campaigns=tuple(
            sorted(
                (_campaign(c, _favor_names(_table(raw, "reset_currency_defines"))) for c in campaign_defines),
                key=lambda c: c.id,
            )
        ),
        adventures=tuple(
            _adventure(a, adventure_defines, campaign_layouts, champion_of)
            for _, a in sorted(adventure_defines.items())
        ),
        patrons={**_PATRONS, **{p.id: p for p in map(_patron, _table(raw, "patron_defines"))}},
    )


# Patron names, used when `patron_defines` isn't among the recorded definitions.
_PATRON_NAMES = {
    i: Patron(i, name) for i, name in enumerate(("Mirt", "Vajra", "Strahd", "Zariel", "Elminster"), 1)
}

# Patron restrictions, used when `patron_defines` isn't loaded (research/push-area-reach.md:
# Mirt good or evil; Vajra CON 14+; Strahd INT 13+; Zariel STR 10+ and CHA 13+; Elminster
# released or reworked in the last 3 years).
_PATRON_RESTRICTIONS = {
    1: (Restriction("only_allow", tags=("good", "evil")),),
    2: (Restriction("only_allow", stats=(("con", ">=", 14),)),),
    3: (Restriction("only_allow", stats=(("int", ">=", 13),)),),
    4: (Restriction("only_allow", stats=(("str", ">=", 10), ("cha", ">=", 13))),),
    5: (Restriction("only_allow", expr="years_since_release_or_rework <= 3"),),
}
_PATRONS = {i: Patron(i, p.name, _PATRON_RESTRICTIONS.get(i, ())) for i, p in _PATRON_NAMES.items()}


def _favor_names(rows: list[dict]) -> dict[int, str]:
    return {int(r["id"]): r.get("name") or "" for r in rows}


def _campaign(define: dict, favor_names: Mapping[int, str] | None = None) -> Campaign:
    properties = define.get("properties") or {}
    event_id = properties.get("event_id") if properties.get("is_event") else None
    currency = define.get("reset_currency_id")
    return Campaign(
        id=int(define["id"]),
        name=define["name"],
        event_id=int(event_id) if event_id is not None else None,
        favor_name=(favor_names or {}).get(int(currency)) if currency else None,
    )


def _patron(define: dict) -> Patron:
    patron_id = int(define["id"])
    return Patron(
        id=patron_id,
        name=define["name"],
        restrictions=_restrictions(define.get("game_changes")) or _PATRON_RESTRICTIONS.get(patron_id, ()),
    )


_RESTRICTION_KINDS = {"only_allow_crusaders": "only_allow", "disallow_crusaders": "disallow"}


def _restrictions(game_changes: list | None) -> tuple[Restriction, ...]:
    found = []
    for change in game_changes or ():
        kind = _RESTRICTION_KINDS.get(change.get("type"))
        if not kind:
            continue
        by_ids, by_tags = change.get("by_ids") or {}, change.get("by_tags") or {}
        by_stat, by_expr = change.get("by_stat") or {}, change.get("by_expr") or {}
        found.append(
            Restriction(
                kind=kind,
                ids=frozenset(int(i) for i in by_ids.get("ids") or ()),
                tags=tuple(t for t in str(by_tags.get("tags") or "").split("|") if t),
                stats=tuple(
                    (str(s["stat"]).lower(), str(s.get("comp") or ">="), int(s["value"]))
                    for s in by_stat.get("stats") or ()
                ),
                stats_any=bool(by_stat.get("pass_any")),
                expr=by_expr.get("expr") or None,
            )
        )
    return tuple(found)


def _forced(game_changes: list | None) -> tuple[ForcedChampion, ...]:
    forced = []
    for change in game_changes or ():
        if change.get("type") != "force_use_heroes":
            continue
        slots = next((s.get("slots") or () for s in change.get("forced_slots") or ()), ())
        levels = change.get("start_levels") or ()
        for i, hero_id in enumerate(change.get("hero_ids") or ()):
            forced.append(
                ForcedChampion(
                    champion_id=int(hero_id),
                    slot=int(slots[i]) if i < len(slots) else None,
                    start_level=int(levels[i]) if i < len(levels) else None,
                )
            )
    return tuple(forced)


def _adventure(
    define: dict,
    all_defines: Mapping[int, dict],
    campaign_layouts: Mapping[int, tuple[Slot, ...]],
    champion_of: Mapping[int, int],
) -> Adventure:
    adventure_id = int(define["id"])
    base_id = int(define["variant_adventure_id"]) if define.get("variant_adventure_id") else None
    campaign_id = int(define["campaign_id"])
    # The adventure's own layout overrides its base adventure's, which overrides the campaign's.
    layout = (
        _layout(define.get("game_changes"))
        or (_layout(all_defines[base_id].get("game_changes")) if base_id in all_defines else ())
        or campaign_layouts.get(campaign_id, ())
    )
    return Adventure(
        id=adventure_id,
        name=define["name"],
        campaign_id=campaign_id,
        base_id=base_id,
        objective_area=_objective_area(define.get("objectives")),
        restrictions_text=_plain_text(define.get("restrictions_text")),
        patron_objectives={
            int(patron_id): area
            for patron_id, tiers in sorted((define.get("patron_objectives") or {}).items())
            if (area := _objective_area(next(iter(tiers.values()), None) if tiers else None))
        },
        layout=layout,
        blocked_slots=_blocked_slots(define.get("game_changes")),
        champion_id=champion_of.get(adventure_id) or champion_of.get(base_id),
        restrictions=_restrictions(define.get("game_changes")),
        forced=_forced(define.get("game_changes")),
    )


_MARKUP = re.compile(r"<[^>]*>")


def _plain_text(text: str | None) -> str:
    """Game text without its rich-text tags, one line per paragraph."""
    lines = _MARKUP.sub("", text or "").replace("\r\n", "\n").split("\n")
    return "\n".join(line.strip() for line in lines if line.strip())


def _objective_area(objectives: list | None) -> int | None:
    areas = [int(o["area"]) for o in objectives or () if o.get("condition") == "complete_area"]
    return max(areas) if areas else None


def _layout(game_changes: list | None) -> tuple[Slot, ...]:
    formation = next((g["formation"] for g in game_changes or () if g.get("type") == "formation"), None)
    return tuple(
        Slot(
            index=i,
            x=int(s["x"]),
            y=int(s["y"]),
            column=int(s.get("col", 0)),
            adjacent=tuple(int(j) for j in s.get("adj") or ()),
        )
        for i, s in enumerate(formation or ())
    )


def _blocked_slots(game_changes: list | None) -> frozenset[int]:
    """Seats an escort occupies. Escorts that arrive by area are treated as always there."""
    blocked: set[int] = set()
    for change in game_changes or ():
        if change.get("type") == "slot_escort":
            blocked.update([change["slot_id"]] if "slot_id" in change else change.get("slot_ids") or ())
        elif change.get("type") == "slot_escort_by_area":
            for escort in change.get("escorts") or ():
                blocked.update(escort.get("slot_ids") or ())
    return frozenset(int(i) for i in blocked)


def _feats(rows: list[dict]) -> dict[int, Feat]:
    return {
        int(row["id"]): Feat(
            id=int(row["id"]),
            champion_id=int(row["hero_id"]),
            name=row.get("name") or "",
            description=row.get("description") or "",
            effects=_effect_strings(row.get("effects")),
        )
        for row in rows
        if row.get("hero_id")
    }


def _effect_strings(effects: list | None) -> tuple[str, ...]:
    return tuple(str(e.get("effect_string") if isinstance(e, dict) else e) for e in effects or () if e)


def _upgrades(rows: list[dict]) -> dict[int, Upgrade]:
    return {
        int(row["id"]): Upgrade(
            id=int(row["id"]),
            champion_id=int(row["hero_id"]),
            required_level=int(row.get("required_level") or 0),
            effect=str(row.get("effect") or ""),
            name=row.get("name") or "",
            is_specialization=bool(row.get("specialization_name")),
        )
        for row in rows
        if row.get("hero_id")
    }


def _effects(rows: list[dict]) -> dict[int, EffectDefinition]:
    return {
        int(row["id"]): EffectDefinition(
            id=int(row["id"]),
            effect_keys=tuple(k for k in row.get("effect_keys") or () if isinstance(k, dict)),
            description=row.get("description") or "",
        )
        for row in rows
    }


def _gear(loot: list[dict]) -> dict[tuple[int, int, int], GearItem]:
    items = (
        GearItem(
            champion_id=int(row["hero_id"]),
            slot=int(row["slot_id"]),
            rarity=int(row["rarity"]),
            name=row.get("name") or "",
            description=row.get("description") or "",
            effects=_effect_strings(row.get("effects")),
        )
        for row in loot
        if row.get("hero_id") and row.get("slot_id") and row.get("rarity")
    )
    return {(i.champion_id, i.slot, i.rarity): i for i in items}


def _table(raw: RawResponses, name: str) -> list[dict]:
    """Every row of one Game Definitions table, wherever the loaded responses carry it.

    A table can come whole (`upgrade_defines`) or sharded (`upgrade_defines_0`, `_1`, ...) in
    `getdefinitions`, and subsets ride along in `getuserdetails.defines` and
    `getcampaigndetails.defines`. Rows are merged by
    id; `getdefinitions` wins over the embedded subset.
    """
    shard = re.compile(rf"{re.escape(name)}(_\d+)?")
    sources = [
        (raw.get("getuserdetails") or {}).get("defines") or {},
        (raw.get("getcampaigndetails") or {}).get("defines") or {},
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
    sheet = hero.get("character_sheet_details") or {}
    age = sheet.get("age")
    return Champion(
        id=int(hero["id"]),
        name=hero["name"],
        seat=int(hero["seat_id"]),
        tags=tags,
        source=_source(hero, tags),
        base_damage=float(hero.get("base_damage") or 0),
        ability_scores={str(k).lower(): int(v) for k, v in (sheet.get("ability_scores") or {}).items()},
        age=int(age) if isinstance(age, (int, float)) or str(age).isdigit() else None,
        date_available=str(hero.get("date_available") or ""),
        last_rework_date=str(hero.get("last_rework_date") or ""),
    )


def _source(hero: dict, tags: frozenset[str]) -> ChampionSource:
    if hero.get("event_name"):
        years = [int(m.group(1)) for t in tags if (m := _YEAR_TAG.fullmatch(t))]
        return ChampionSource("event", hero["event_name"], min(years) if years else None)
    return ChampionSource("core" if "core" in tags else "chest")
