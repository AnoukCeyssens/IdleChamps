""""What would make this better": the one-change engine (#34; Next Steps reuse it).

A change is one action: a gear chest or blacksmith contract on one champion's slot, a feat, an
unowned champion, one modron core level, or a potion (potions only as hints, never Next Steps and
never in a recommendation's baseline). Its gain is measured by re-running `recommend` with that one
change applied, against the recommendation it would improve.

Re-searching every change is too slow, so a cheap upper bound comes first. It starts from the
assignment bound of every Main DPS seat in the original search (`formation_search.plan_bounds`):

- a change to champion X only changes X's seat options, so a plan around another Main DPS can rise
  by at most the most X's best seat bound rises in any slot (an exchange argument on the
  assignment); plans with X as the Main DPS are re-bounded directly;
- a potion lifts every plan by what it adds to the Main DPS's own score;
- a change the models don't see at all (X's loadouts and effects unchanged) can't matter;
- a modron core level re-bounds every plan (the models may read cores; #29).

A change is re-searched only if its bound could beat the recommendation's score; with a `limit`,
changes are re-searched best bound first and the rest are dropped once no bound can reach the
`limit`-th best gain found. This relies on `Scorer.bound` holding its contract (never below what a
seat adds) and on a champion's changes reaching other seats only through that champion's own
effects; per-champion reconstructions with interactions (#40) must keep both true.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace

from advisor.account import RARITY_NAMES, Account, OwnedChampion, OwnedGear
from advisor.catalogue import GEAR_SLOTS, Catalogue
from advisor.champion_models import ChampionModels, Seat
from advisor.formation_search import Candidate, plan_bounds
from advisor.recommendation import FormationRecommendation, recommend
from advisor.restrictions import failed

# Item levels one contract adds: a Large Blacksmithing Contract's 24 (community value; the game spreads
# them over the champion's slots, a hint puts them on one).
CONTRACT_LEVELS = 24
# Chests drop up to Epic.
MAX_CHEST_RARITY = 4
# One modron core level, in core experience. The level curve isn't in the recording (no
# `modron_core_defines`); no Champion Model reads core experience yet, so these hints measure 0.
MODRON_LEVEL_EXP = 1000
# Gains at or below this (%) count as zero and are never shown.
MIN_GAIN_PCT = 0.01
# Hints shown in a recommendation's right rail.
RAIL_HINTS = 10

_SLACK = 1e-5  # the assignment's fill tie-break, so the bound stays an over-estimate


@dataclass(frozen=True)
class Change:
    kind: str  # "gear" | "feat" | "champion" | "modron" | "potion"
    action: str  # what the player does, e.g. "Open a chest for Celeste's slot 2 (Rare → Epic)"
    champion_id: int | None = None
    gear: OwnedGear | None = None  # the item after the change (gear)
    feat_id: int | None = None
    core_id: int | None = None
    core_exp: int | None = None  # the core's experience after the change
    potion_effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hint:
    change: Change
    gain_pct: float  # % gain of the Run Goal's score on this adventure
    recommendation: FormationRecommendation  # the re-searched recommendation with the change


def candidate_changes(catalogue: Catalogue, account: Account, *, potions: bool = True) -> tuple[Change, ...]:
    """Every one-change the account could make. `potions=False` for Next Steps."""
    changes: list[Change] = []
    for champion_id, owned in sorted(account.champions.items()):
        name = catalogue.champion(champion_id).name if catalogue.find(champion_id) else f"Champion {champion_id}"
        for slot in GEAR_SLOTS:
            held = owned.gear.get(slot)
            rarity = held.rarity if held else 0
            if rarity < MAX_CHEST_RARITY and catalogue.gear_item(champion_id, slot, rarity + 1):
                better = OwnedGear(slot, rarity + 1, held.level if held else 1, held.gild if held else 0)
                before = RARITY_NAMES.get(rarity, "empty")
                changes.append(Change(
                    "gear", f"Open a chest for {name}'s slot {slot} ({before} → {RARITY_NAMES[rarity + 1]})",
                    champion_id, gear=better,
                ))
            if held and catalogue.gear_item(champion_id, slot, held.rarity):
                levelled = replace(held, level=held.level + CONTRACT_LEVELS)
                changes.append(Change(
                    "gear", f"Blacksmith contract on {name}'s slot {slot} (item level {held.level} → {levelled.level})",
                    champion_id, gear=levelled,
                ))
        for feat in catalogue.feats_of(champion_id):
            if feat.id not in owned.feats:
                changes.append(Change("feat", f"Unlock {name}'s feat {feat.name}", champion_id, feat_id=feat.id))
    for champion in catalogue.champions:
        if not account.owns(champion.id):
            changes.append(Change("champion", f"Get {champion.name} ({champion.source.label})", champion.id))
    for core_id, exp in sorted(account.modron_cores.items()):
        changes.append(Change("modron", f"Level up modron core {core_id}", core_id=core_id, core_exp=exp + MODRON_LEVEL_EXP))
    if potions:
        for potion in sorted(catalogue.potions.values(), key=lambda p: p.id):
            changes.append(Change("potion", f"Use a {potion.name}", potion_effects=potion.effects))
    return tuple(changes)


def what_if(
    catalogue: Catalogue, account: Account, change: Change, *, statuses=None
) -> tuple[Account, ChampionModels]:
    """The account with the change made, and Champion Models for it (`statuses`: the Model Statuses
    to carry over, `ChampionModels.statuses`)."""
    champions = dict(account.champions)
    if change.kind == "gear":
        owned = champions[change.champion_id]
        champions[change.champion_id] = replace(owned, gear={**owned.gear, change.gear.slot: change.gear})
    elif change.kind == "feat":
        owned = champions[change.champion_id]
        champions[change.champion_id] = replace(owned, feats=(*owned.feats, change.feat_id))
    elif change.kind == "champion":
        champions[change.champion_id] = OwnedChampion(change.champion_id, level=1)
    changed = replace(account, champions=champions)
    if change.kind == "modron":
        changed = replace(changed, modron_cores={**account.modron_cores, change.core_id: change.core_exp})
    return changed, ChampionModels(catalogue, changed, potions=change.potion_effects, statuses=statuses)


def measure(
    catalogue: Catalogue,
    account: Account,
    rec: FormationRecommendation,
    changes: Iterable[Change],
    *,
    models: ChampionModels | None = None,
    limit: int | None = None,
    solver: str = "fast",
    budget_s: float = 0.1,
) -> tuple[Hint, ...]:
    """Each change's gain on this recommendation's adventure and Run Goal, best first; changes with
    no gain are left out. With `limit`, only the best `limit`."""
    if rec.problem is None or not rec.score:
        return ()
    models = models or ChampionModels(catalogue, account)
    bounder = _Bounder(catalogue, account, rec, models)
    found = math.log(rec.score)
    threshold = found + math.log1p(MIN_GAIN_PCT / 100) - _SLACK
    promising = []
    for change in changes:
        changed, changed_models = what_if(catalogue, account, change, statuses=models.statuses)
        bound = bounder.bound(change, changed_models)
        if bound > threshold:
            promising.append((bound, change, changed, changed_models))
    promising.sort(key=lambda p: -p[0])

    hints: list[Hint] = []
    patron = rec.patron.id if rec.patron else None
    for bound, change, changed, changed_models in promising:
        if limit is not None and len(hints) >= limit:
            worst = math.log1p(hints[limit - 1].gain_pct / 100) + found
            if bound <= worst + _SLACK:
                break  # sorted by bound: no later change can enter the best `limit`
        after = recommend(
            catalogue, changed, rec.adventure, rec.run_goal, patron, models=changed_models, solver=solver, budget_s=budget_s
        )
        if not after.score:
            continue
        gain = (math.exp(math.log(after.score) - found) - 1) * 100
        if gain > MIN_GAIN_PCT:
            hints.append(Hint(change, gain, after))
            hints.sort(key=lambda h: -h.gain_pct)
    return tuple(hints[:limit] if limit is not None else hints)


def hints(
    catalogue: Catalogue, account: Account, rec: FormationRecommendation, *, models: ChampionModels | None = None
) -> tuple[Hint, ...]:
    """The right rail's "What would make this better": the best RAIL_HINTS changes, potions included."""
    return measure(catalogue, account, rec, candidate_changes(catalogue, account), models=models, limit=RAIL_HINTS)


class _Bounder:
    """Cheap upper bounds (log score) on the recommendation's best score after one change."""

    def __init__(self, catalogue: Catalogue, account: Account, rec: FormationRecommendation, models: ChampionModels):
        self.catalogue = catalogue
        self.account = account
        self.rec = rec
        self.models = models
        self.problem = rec.problem
        self._base_scorers: dict[Seat, object] = {}  # shared across changes, so their bound caches are reused
        self.plans = plan_bounds(self.problem, self._base_scorer)
        self.by_id = {c.champion_id: c for c in self.problem.candidates}
        self.rules = (*rec.adventure.restrictions, *(rec.patron.restrictions if rec.patron else ()))
        ids = {c.champion_id for c in self.problem.candidates}
        self.anyone_leads = self.problem.main_dps_ids >= ids  # no dps-tagged candidate: anyone may be Main DPS

    def _base_scorer(self, main: Seat):
        if main not in self._base_scorers:
            self._base_scorers[main] = self.models.scorer(self.rec.adventure.layout, self.rec.run_goal, main)
        return self._base_scorers[main]

    def _scorer_for(self, models: ChampionModels):
        layout, goal = self.rec.adventure.layout, self.rec.run_goal
        return lambda main: models.scorer(layout, goal, main)

    def bound(self, change: Change, changed: ChampionModels) -> float:
        if change.kind == "potion":
            return self._potion(changed)
        if change.kind == "modron":
            return max(plan_bounds(self.problem, self._scorer_for(changed)).values(), default=-math.inf)
        return self._champion(change.champion_id, changed)

    def _potion(self, changed: ChampionModels) -> float:
        if not changed.potion_effects():
            return -math.inf
        old, new = self._base_scorer, self._scorer_for(changed)
        return max(
            (b + new(m).score((m,)).value - old(m).score((m,)).value for m, b in self.plans.items()), default=-math.inf
        )

    def _champion(self, champion_id: int, changed: ChampionModels) -> float:
        champion = self.catalogue.find(champion_id)
        before = self.by_id.get(champion_id)
        if champion is None:
            return -math.inf
        if before is None and (self.account.owns(champion_id) or failed(self.rules, champion, self.account.server_time)):
            return -math.inf  # restricted out: the re-search wouldn't seat it either
        loadouts = changed.loadouts(champion_id)
        if before is not None and loadouts == before.loadouts and all(
            changed.effects(champion_id, l) == self.models.effects(champion_id, l) for l in loadouts
        ):
            return -math.inf  # the models see no difference
        after = Candidate(
            champion_id, champion.seat, loadouts, before.forced if before else False, before.forced_slot if before else None
        )
        slots = [after.forced_slot] if after.forced_slot is not None else [s.index for s in self.problem.slots]
        old, new = self._base_scorer, self._scorer_for(changed)
        seat_of = {c.champion_id: c.seat for c in self.problem.candidates}

        best = -math.inf
        for main, plan in self.plans.items():
            if main.champion_id == champion_id:
                continue
            rise = 0.0
            if seat_of[main.champion_id] != champion.seat:
                new_scorer, old_scorer = new(main), old(main)
                for slot in slots:
                    if slot == main.slot:
                        continue
                    was = max((old_scorer.bound(Seat(champion_id, slot, l)) for l in before.loadouts), default=0.0) if before else 0.0
                    now = max(new_scorer.bound(Seat(champion_id, slot, l)) for l in loadouts)
                    rise = max(rise, now - was)
            best = max(best, plan + rise)
        if "dps" in champion.tags or self.anyone_leads:
            others = tuple(c for c in self.problem.candidates if c.champion_id != champion_id)
            leading = replace(self.problem, candidates=(*others, after), main_dps_ids=frozenset({champion_id}))
            best = max(best, *plan_bounds(leading, new).values(), -math.inf)
        return best + _SLACK
