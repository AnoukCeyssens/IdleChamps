"""Explorer: the Formation view's navigation, built from the Catalogue and the Account.

Decides what the tree shows for each adventure (completion, Not running); the web layer only
draws it.
"""

from __future__ import annotations

from dataclasses import dataclass

from advisor.account import Account
from advisor.catalogue import Adventure, Campaign, Catalogue

RUN_GOALS = ("Push", "Gem Farm", "Gold Farm")


@dataclass(frozen=True)
class Completion:
    kind: str  # "done" | "best" | "not-run" | "unknown"
    best_area: int | None = None
    tier: int | None = None  # event champion's highest finished tier, for "unknown"

    @property
    def label(self) -> str:
        if self.kind == "done":
            return "Done"
        if self.kind == "best":
            return f"Best area {self.best_area}"
        if self.kind == "not-run":
            return "Not run"
        if self.tier is None:
            return "Completion unknown"
        return f"Completion unknown · {f'Tier {self.tier} done' if self.tier else 'no tier done'}"


@dataclass(frozen=True)
class AdventureNode:
    adventure: Adventure
    completion: Completion
    running: bool
    variants: tuple[AdventureNode, ...] = ()


@dataclass(frozen=True)
class CampaignNode:
    campaign: Campaign
    running: bool
    adventures: tuple[AdventureNode, ...]


def is_running(catalogue: Catalogue, account: Account, adventure: Adventure) -> bool:
    """Free play is always startable; an event's adventures only while it is the Running Event."""
    campaign = catalogue.campaign(adventure.campaign_id)
    return not campaign.is_event or campaign.event_id in account.running_event_ids


def completion(catalogue: Catalogue, account: Account, adventure: Adventure) -> Completion:
    if not is_running(catalogue, account, adventure):
        # Completion of events that aren't running isn't in the Player Snapshot: never "not done".
        tier = account.event_tiers.get(adventure.champion_id) if adventure.champion_id else None
        return Completion("unknown", tier=tier)
    best = account.best_areas.get(adventure.id)
    reached = adventure.objective_area is not None and best is not None and best >= adventure.objective_area
    if adventure.id in account.completed_adventure_ids or reached:
        return Completion("done", best_area=best)
    if best:
        return Completion("best", best_area=best)
    return Completion("not-run")


def adventure_node(catalogue: Catalogue, account: Account, adventure: Adventure) -> AdventureNode:
    return AdventureNode(
        adventure=adventure,
        completion=completion(catalogue, account, adventure),
        running=is_running(catalogue, account, adventure),
        variants=tuple(adventure_node(catalogue, account, v) for v in catalogue.variants_of(adventure.id)),
    )


def tree(catalogue: Catalogue, account: Account) -> tuple[CampaignNode, ...]:
    """Campaigns (free play first, then events), each with its base adventures and their variants.
    A variant whose base adventure isn't in the Catalogue is listed at the top level."""
    known = {a.id for a in catalogue.adventures}
    by_campaign: dict[int, list[Adventure]] = {}
    for adventure in catalogue.adventures:
        if adventure.base_id is None or adventure.base_id not in known:
            by_campaign.setdefault(adventure.campaign_id, []).append(adventure)
    campaigns = sorted(
        (catalogue.campaign(cid) for cid in by_campaign), key=lambda c: (c.is_event, c.is_event and c.name, c.id)
    )
    return tuple(
        CampaignNode(
            campaign=c,
            running=not c.is_event or c.event_id in account.running_event_ids,
            adventures=tuple(adventure_node(catalogue, account, a) for a in by_campaign[c.id]),
        )
        for c in campaigns
    )
