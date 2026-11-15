"""Account: the parts of the Player Snapshot the advisor uses, as typed domain objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from advisor.request_log import RawResponses


@dataclass(frozen=True)
class OwnedChampion:
    id: int
    level: int


@dataclass(frozen=True)
class Account:
    champions: Mapping[int, OwnedChampion]  # owned champions by champion id

    def owns(self, champion_id: int) -> bool:
        return champion_id in self.champions


def parse_account(raw: RawResponses) -> Account:
    details = raw["getuserdetails"]["details"]
    owned = (
        OwnedChampion(id=int(h["hero_id"]), level=int(h["level"]))  # numbers arrive as strings
        for h in details["heroes"]
        if str(h.get("owned")) == "1"
    )
    return Account(champions={c.id: c for c in owned})
