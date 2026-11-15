"""Account: the parts of the Player Snapshot the advisor uses, as typed domain objects."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from advisor.request_log import RawResponses


@dataclass(frozen=True)
class OwnedChampion:
    id: int
    level: int


@dataclass(frozen=True)
class Account:
    champions: Mapping[int, OwnedChampion]  # owned champions by champion id
    completed_adventure_ids: frozenset[int] = frozenset()  # free play, variants, Running Event
    best_areas: Mapping[int, int] = field(default_factory=dict)  # adventure id -> highest area
    event_tiers: Mapping[int, int] = field(default_factory=dict)  # event champion id -> tier finished
    unlocked_patron_ids: frozenset[int] = frozenset()
    running_event_ids: frozenset[int] = frozenset()

    def owns(self, champion_id: int) -> bool:
        return champion_id in self.champions


def parse_account(raw: RawResponses) -> Account:
    details = raw["getuserdetails"]["details"]
    owned = (
        OwnedChampion(id=int(h["hero_id"]), level=int(h["level"]))  # numbers arrive as strings
        for h in details["heroes"]
        if str(h.get("owned")) == "1"
    )
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
    )


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
