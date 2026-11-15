"""How the advisor uses one champion (champion page, #37): the Formation Recommendations that seat
it, with its seat and choices there and a reason where those choices differ per Run Goal, and the
Next Steps that involve it.

Reads recommendations and Next Steps already computed; computes nothing itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from advisor.catalogue import Adventure, Feat, Patron, SpecializationOption
from advisor.champion_models import ChampionExplanation, EffectLine
from advisor.explorer import RUN_GOALS
from advisor.next_steps import NextStep
from advisor.recommendation import RUN_GOAL_METRICS, FormationRecommendation


@dataclass(frozen=True)
class SeatedIn:
    """One recommendation (adventure × Run Goal × patron) that seats the champion."""

    adventure: Adventure
    run_goal: str
    patron: Patron | None
    slot: int
    is_main_dps: bool
    contribution_label: str
    specializations: tuple[SpecializationOption, ...]
    feats: tuple[Feat, ...]
    stale: bool  # from before the running Recompute
    lent: bool  # a forced champion the player doesn't own
    reason: str | None = None  # why its choices here differ from another Run Goal's on this adventure


def seated_in(
    champion_id: int,
    recommendations: Iterable[tuple[FormationRecommendation, bool]],
    explanation: ChampionExplanation,
) -> tuple[SeatedIn, ...]:
    """Every (recommendation, stale) that seats the champion, by adventure, patron, then Run Goal."""
    found = []
    for rec, stale in recommendations:
        seat = next((s for s in rec.seats if s.champion is not None and s.champion.id == champion_id), None)
        if seat is None:
            continue
        found.append(SeatedIn(
            adventure=rec.adventure, run_goal=rec.run_goal, patron=rec.patron, slot=seat.slot,
            is_main_dps=seat.is_main_dps, contribution_label=seat.contribution_label,
            specializations=seat.specializations, feats=seat.feats, stale=stale, lent=seat.provided_by_adventure,
        ))
    order = {g: i for i, g in enumerate(RUN_GOALS)}
    found.sort(key=lambda s: (s.adventure.id, s.patron.id if s.patron else -1, order.get(s.run_goal, len(order))))
    return tuple(_with_reasons(found, explanation))


def champion_steps(champion_id: int, steps: Sequence[NextStep]) -> tuple[NextStep, ...]:
    """The Next Steps that involve the champion (its gear, feats, or getting it), best first."""
    return tuple(s for s in steps if s.change.champion_id == champion_id)


def _with_reasons(found: list[SeatedIn], explanation: ChampionExplanation) -> list[SeatedIn]:
    groups: dict[tuple, list[int]] = {}
    for i, s in enumerate(found):
        groups.setdefault((s.adventure.id, s.patron.id if s.patron else None), []).append(i)
    for members in groups.values():
        choices = [_choices(found[i]) for i in members]
        if len({frozenset(c) for c in choices}) < 2:
            continue
        common = set.intersection(*(set(c) for c in choices))
        for i, mine in zip(members, choices):
            s = found[i]
            own = [c for c in mine if c not in common]
            metric = RUN_GOAL_METRICS.get(s.run_goal, s.run_goal)
            if own:
                picks = ", ".join(f"{name} ({_effect_text(lines)})" for _, name, lines in (
                    _describe(c, explanation) for c in own))
                reason = f"{s.run_goal} picks {picks} for {metric}"
            else:
                reason = f"{s.run_goal}: no further choice adds to {metric} here"
            found[i] = SeatedIn(**{**s.__dict__, "reason": reason})
    return found


def _choices(s: SeatedIn) -> list[tuple[str, int, str]]:
    return [("spec", o.id, o.name) for o in s.specializations] + [("feat", f.id, f.name) for f in s.feats]


def _describe(choice: tuple[str, int, str], explanation: ChampionExplanation) -> tuple[str, str, tuple[EffectLine, ...]]:
    kind, ident, name = choice
    table = explanation.specializations if kind == "spec" else explanation.feats
    return kind, name, table.get(ident, ())


def _effect_text(lines: tuple[EffectLine, ...]) -> str:
    if not lines:
        return "no effect listed"
    return "; ".join(line.modelled or line.assumption or "not modelled" for line in lines)
