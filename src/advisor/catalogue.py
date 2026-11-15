"""Catalogue: the parts of the Game Definitions the advisor uses, as typed domain objects."""

from __future__ import annotations

import re
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
class Campaign:
    """A free play campaign, or an event (an event's adventures form a campaign of their own)."""

    id: int
    name: str
    event_id: int | None = None  # set on event campaigns

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

    @property
    def is_variant(self) -> bool:
        return self.base_id is not None


@dataclass(frozen=True)
class Patron:
    id: int
    name: str


@dataclass(frozen=True)
class Catalogue:
    champions: tuple[Champion, ...]  # ordered by seat, then id
    campaigns: tuple[Campaign, ...] = ()
    adventures: tuple[Adventure, ...] = ()  # ordered by id
    patrons: Mapping[int, Patron] = field(default_factory=dict)

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


def parse_catalogue(raw: RawResponses) -> Catalogue:
    champions = [
        _champion(h)
        for h in raw["getdefinitions"]["hero_defines"]
        if h.get("seat_id")  # seat 0 marks the game's "Please Reload" placeholders
    ]
    defines = _defines(raw)
    campaigns = {c.id: c for c in map(_campaign, defines["campaign_defines"].values())}
    campaign_layouts = {
        int(c["id"]): _layout(c.get("game_changes")) for c in defines["campaign_defines"].values()
    }
    champion_of = {
        int(ref["adventure_id"]): int(h["id"])
        for h in raw["getdefinitions"]["hero_defines"]
        for ref in h.get("adventure_ids") or ()
        if h.get("event_name")
    }
    adventure_defines = defines["adventure_defines"]
    adventures = [
        _adventure(a, adventure_defines, campaign_layouts, champion_of)
        for _, a in sorted(adventure_defines.items())
    ]
    patrons = {p.id: p for p in map(_patron, defines["patron_defines"].values())}
    return Catalogue(
        champions=tuple(sorted(champions, key=lambda c: (c.seat, c.id))),
        campaigns=tuple(sorted(campaigns.values(), key=lambda c: c.id)),
        adventures=tuple(adventures),
        patrons={**_PATRON_NAMES, **patrons},
    )


# Patron names, used when `patron_defines` isn't among the recorded definitions.
_PATRON_NAMES = {
    i: Patron(i, name) for i, name in enumerate(("Mirt", "Vajra", "Strahd", "Zariel", "Elminster"), 1)
}

_DEFINE_TABLES = ("adventure_defines", "campaign_defines", "patron_defines")


def _defines(raw: RawResponses) -> dict[str, dict[int, dict]]:
    """Adventure, campaign and patron defines by id, merged from every response that embeds
    them. `getdefinitions` is the full catalogue and wins; the others hold subsets."""
    sources = [
        (raw.get("getuserdetails") or {}).get("defines") or {},
        (raw.get("getcampaigndetails") or {}).get("defines") or {},
        raw.get("getdefinitions") or {},
    ]
    return {
        table: {int(d["id"]): d for source in sources for d in source.get(table) or ()}
        for table in _DEFINE_TABLES
    }


def _campaign(define: dict) -> Campaign:
    properties = define.get("properties") or {}
    event_id = properties.get("event_id") if properties.get("is_event") else None
    return Campaign(
        id=int(define["id"]),
        name=define["name"],
        event_id=int(event_id) if event_id is not None else None,
    )


def _patron(define: dict) -> Patron:
    return Patron(id=int(define["id"]), name=define["name"])


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
