"""Explorer: the Formation view's navigation, built from the Catalogue and the Account.

Decides what the tree shows for each adventure (completion, Not running); the web layer only
draws it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

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


def is_unlocked(catalogue: Catalogue, account: Account, adventure: Adventure) -> bool:
    """Can I start this adventure now? It must be startable (`is_running`) and meet its
    `requirements`: completed adventures, a count of completions in a campaign, a date. Other
    conditions (event year unlocks, champion availability) aren't in the snapshot and count as met.
    An adventure I have a best area in, or a game instance on, was unlocked."""
    if not is_running(catalogue, account, adventure):
        return False
    if account.best_areas.get(adventure.id) or any(i.adventure_id == adventure.id for i in account.instances):
        return True  # played before: it was unlocked
    for requirement in adventure.requirements:
        if not isinstance(requirement, dict):
            continue
        condition = requirement.get("condition")
        if condition == "adventure_complete":
            needed = catalogue.adventure(int(requirement.get("adventure_id") or 0))
            if needed is None or not _is_done(account, needed):
                return False
        elif condition == "adventures_completed":
            campaign_id = int(requirement.get("campaign_id") or 0)
            done = sum(1 for a in catalogue.adventures if a.campaign_id == campaign_id and _is_done(account, a))
            if done < int(requirement.get("count") or 0):
                return False
        elif condition == "date" and account.server_time is not None:
            now = datetime.fromtimestamp(account.server_time, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if str(requirement.get("date") or "") > now:
                return False
    return True


def same_area_set_to_load(catalogue: Catalogue, account: Account, adventure: Adventure) -> Adventure | None:
    """An unlocked adventure sharing `adventure`'s Area Set, to load in-game so a Refresh brings its
    monster placement: the adventure itself if possible, then base adventures before variants."""
    if adventure.area_set_id is None:
        return None
    sharing = [a for a in catalogue.adventures if a.area_set_id == adventure.area_set_id]
    sharing.sort(key=lambda a: (a.id != adventure.id, a.is_variant, a.id))
    return next((a for a in sharing if is_unlocked(catalogue, account, a)), None)


def is_done(account: Account, adventure: Adventure) -> bool:
    """Its objective is completed (as far as the Player Snapshot shows)."""
    return _is_done(account, adventure)


def patron_completion(account: Account, adventure: Adventure, patron_id: int) -> bool | None:
    """Whether `adventure`'s objective under `patron_id` is completed, None when the snapshot can't
    tell. `completed_patron_adventure_ids` (per campaign) has an unverified element shape (#9): only
    shapes naming both the patron and the adventure are read, `{patron id: [adventure id, ...]}` and
    `[{"patron_id": ..., "adventure_id": ...}, ...]`. Anything else (bare ids, strings, other
    nesting) is unknown, never an error."""
    sent = account.completed_patron_adventures.get(adventure.campaign_id)
    try:
        if isinstance(sent, dict):
            ids = sent.get(str(patron_id), sent.get(patron_id))
            if isinstance(ids, list) and all(_is_int(i) for i in ids):
                return adventure.id in {int(i) for i in ids}
            return None
        if isinstance(sent, list) and sent and all(isinstance(e, dict) for e in sent):
            pairs = {
                (int(e["patron_id"]), int(e["adventure_id"])) for e in sent if "patron_id" in e and "adventure_id" in e
            }
            return (patron_id, adventure.id) in pairs if len(pairs) == len(sent) else None
    except (TypeError, ValueError):
        return None
    return None


def _is_int(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


def _is_done(account: Account, adventure: Adventure) -> bool:
    best = account.best_areas.get(adventure.id)
    return adventure.id in account.completed_adventure_ids or (
        adventure.objective_area is not None and best is not None and best >= adventure.objective_area
    )
