"""Community and saved formation comparisons (spec #22 module 9, story 44).

Every community formation (`community.matching`) and saved formation (`Account.saved_formations`)
that matches the adventure and Run Goal is scored by the same model as the recommendation and shown
as "X% weaker by our model" with the 1–2 effects that explain the gap. The legal ones also seed the
Formation Search, so the recommendation is never weaker than them by the model's own score.

A saved formation matches every Run Goal of the adventures sharing its formation-save campaign (the
adventure's `formation_saves_campaign_id` game change, else its campaign) and layout size.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from advisor import community
from advisor.account import Account
from advisor.catalogue import Adventure, Catalogue
from advisor.champion_models import GOLD_PER_DAMAGE_EXPONENT, ChampionModels, Contribution, Loadout, Seat
from advisor.formation_search import ScorerFor, SearchProblem, SearchResult, evaluate

MODEL_REVIEW_GAP_PCT = 10.0  # a community Gem Farm formation further below the recommendation raises a model review flag
_EFFECT_FLOOR = 1e-3  # log units: smaller differences explain nothing


@dataclass(frozen=True)
class Comparison:
    source: str  # "community" | "saved"
    name: str
    pct_weaker: float | None  # % below the recommendation by the model (negative: stronger); None: not comparable
    top_effects: tuple[str, ...] = ()  # the 1–2 effects that explain the difference
    unavailable: str | None = None  # why it can't be compared here
    citation: str | None = None  # where a community formation comes from (pinned)
    run_goal: str = ""

    @property
    def label(self) -> str:
        return f"{'Community' if self.source == 'community' else 'Saved'} formation “{self.name}”"

    @property
    def verdict(self) -> str:
        if self.pct_weaker is None:
            return f"not comparable: {self.unavailable}"
        if abs(self.pct_weaker) < 0.5:
            return "as strong as the recommendation"
        if self.pct_weaker < 0:
            return f"{-self.pct_weaker:.0f}% stronger by our model"
        return f"{self.pct_weaker:.0f}% weaker by our model"

    @property
    def raises_model_review(self) -> bool:
        return (
            self.source == "community" and self.run_goal == "Gem Farm"
            and self.pct_weaker is not None and self.pct_weaker > MODEL_REVIEW_GAP_PCT
        )


@dataclass(frozen=True)
class Given:
    """A community or saved formation, resolved against one search problem."""

    source: str
    name: str
    citation: str | None
    placements: tuple[tuple[int, int, tuple[Loadout, ...]], ...] | None  # None: not legal here
    unavailable: str | None

    @property
    def seed(self) -> tuple[Seat, ...] | None:
        return tuple(Seat(c, s, loadouts[0]) for c, s, loadouts in self.placements) if self.placements else None


def given_formations(
    catalogue: Catalogue, account: Account, adventure: Adventure, run_goal: str, problem: SearchProblem
) -> tuple[Given, ...]:
    found = []
    for f in community.matching(catalogue, adventure, run_goal):
        seated = [(c, slot) for slot, c in enumerate(f.formation) if c > 0]
        found.append(_resolve(catalogue, account, problem, "community", f.name, f.source.url, seated, f.specializations, None))
    for f in account.saved_formations:
        if f.campaign_id != saves_campaign(adventure) or len(f.formation) != len(adventure.layout):
            continue
        seated = [(c, slot) for slot, c in enumerate(f.formation) if c > 0]
        found.append(_resolve(catalogue, account, problem, "saved", f.name, None, seated, f.specializations, f.feats))
    return tuple(found)


def saves_campaign(adventure: Adventure) -> int:
    for change in adventure.game_changes:
        if isinstance(change, dict) and change.get("type") == "formation_saves_campaign_id" and change.get("id") is not None:
            return int(change["id"])
    return adventure.campaign_id


def compare(
    catalogue: Catalogue, models: ChampionModels, run_goal: str, given: tuple[Given, ...],
    problem: SearchProblem, scorer_for: ScorerFor, recommended: SearchResult,
) -> tuple[Comparison, ...]:
    out = []
    for g in given:
        found = evaluate(problem, scorer_for, g.placements) if g.placements else None
        if g.placements and found is None:
            g = Given(g.source, g.name, g.citation, None, "it seats no champion the model can use as Main DPS")
        if found is None:
            out.append(Comparison(g.source, g.name, None, (), g.unavailable, g.citation, run_goal))
            continue
        excess = recommended.score.value - found.score.value
        pct = (1 - math.exp(-min(excess, 700))) * 100 if excess >= 0 else -math.expm1(min(-excess, 700)) * 100
        effects = _explain(catalogue, models, run_goal, recommended, found) if pct >= 0.5 else ()
        out.append(Comparison(g.source, g.name, pct, effects, None, g.citation, run_goal))
    return tuple(out)


def _resolve(catalogue, account, problem, source, name, citation, seated, specs: Mapping, feats: Mapping | None) -> Given:
    names = lambda ids: ", ".join(catalogue.champion(i).name if catalogue.find(i) else f"champion {i}" for i in ids)
    candidates = {c.champion_id: c for c in problem.candidates}
    usable = {s.index for s in problem.slots}
    unknown = [c for c, _ in seated if catalogue.find(c) is None]
    missing = [c for c, _ in seated if c not in unknown and not account.owns(c) and c not in candidates]
    barred = [c for c, _ in seated if c not in unknown and c not in missing and c not in candidates]
    blocked = sorted(s for _, s in seated if s not in usable)
    here = dict(seated)
    left = [c.champion_id for c in problem.candidates if c.forced and (
        c.champion_id not in here or (c.forced_slot is not None and here[c.champion_id] != c.forced_slot))]
    seat_numbers = [candidates[c].seat for c, _ in seated if c in candidates]
    reason = (
        f"needs {names(missing)} (not owned)" if missing
        else f"{names(barred)} not allowed here" if barred
        else f"slot {', '.join(map(str, blocked))} is blocked or missing here" if blocked
        else f"leaves out {names(left)}, whom the adventure puts in the formation" if left
        else f"unknown champion {', '.join(map(str, unknown))}" if unknown
        else "two champions share a seat number" if len(seat_numbers) != len(set(seat_numbers))
        else None
    )
    if reason:
        return Given(source, name, citation, None, reason)
    placements = tuple((c, slot, _loadouts(candidates[c].loadouts, specs.get(c, ()), feats.get(c) if feats else None))
                       for c, slot in seated)
    return Given(source, name, citation, placements, None)


def _loadouts(options: tuple[Loadout, ...], specs, feats) -> tuple[Loadout, ...]:
    """The loadouts that keep the formation's specializations (and its feats, for a saved one); all
    of them when none does (an option the model doesn't know)."""
    keep = [l for l in options if set(specs) <= set(l.specializations)] or list(options)
    if feats is not None:
        keep = [l for l in keep if set(l.feats) <= set(feats)] or keep
    return tuple(keep)


def _explain(catalogue: Catalogue, models: ChampionModels, run_goal: str, rec: SearchResult, other: SearchResult) -> tuple[str, ...]:
    """The 1–2 biggest reasons `other` scores below `rec`, in placement-score terms."""
    damage_w, gold_w = (GOLD_PER_DAMAGE_EXPONENT, 1.0) if run_goal == "Gold Farm" else (1.0, 0.0)

    def weight(c: Contribution | None) -> float:
        if c is None:
            return 0.0
        return damage_w * math.log1p(c.damage_pct / 100) + gold_w * math.log1p(c.gold_pct / 100)

    def label(c: Contribution, main_name: str) -> str:
        text = f"+{_pct(c.damage_pct)} to {main_name}"
        return text + (f", +{_pct(c.gold_pct)} gold" if run_goal == "Gold Farm" and c.gold_pct >= 0.5 else "")

    name = lambda i: catalogue.champion(i).name
    rec_main, other_main = rec.main_dps.champion_id, other.main_dps.champion_id
    reasons: list[tuple[float, str]] = []
    # Gem Farm is anchored on the Main DPS's own reach (`champion_models.GemFarm`): only the buffs it
    # gets count there, not its base damage.
    if rec_main != other_main and run_goal != "Gem Farm":
        w = damage_w * (math.log(models.base_damage(rec_main)) - math.log(models.base_damage(other_main)))
        reasons.append((w, f"Main DPS {name(other_main)} instead of {name(rec_main)}"))
    ours, theirs = rec.score.contributions, other.score.contributions
    for champion_id in dict.fromkeys((*ours, *theirs)):
        if champion_id in (rec_main, other_main) and rec_main != other_main:
            continue  # part of the Main DPS difference
        w = weight(ours.get(champion_id)) - weight(theirs.get(champion_id))
        if champion_id not in theirs:
            text = f"{name(champion_id)} ({label(ours[champion_id], name(rec_main))}) is not in this formation"
        elif champion_id not in ours:
            text = f"{name(champion_id)} adds only {label(theirs[champion_id], name(other_main))} here"
        else:
            text = (f"{name(champion_id)} adds {label(theirs[champion_id], name(other_main))} here, "
                    f"{label(ours[champion_id], name(rec_main))} in the recommendation")
        reasons.append((w, text))
    if rec.setup is not None and other.setup is not None:
        lift = lambda r: r.score.value - (r.placement.value if r.placement else r.score.value)
        on_field = lambda r: sum(1 for f in r.setup.familiars if f is not None)
        w = lift(rec) - lift(other)
        reasons.append((w, f"Familiars on the field: {on_field(other)} here, {on_field(rec)} in the recommendation"))
    reasons.sort(key=lambda r: -r[0])
    return tuple(text for w, text in reasons[:2] if w > _EFFECT_FLOOR)


def _pct(pct: float) -> str:
    return f"{pct:.0f}%" if pct < 1e5 else f"{pct:.2e}%".replace("e+", "e")
