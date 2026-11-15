"""Formation Recommendation: `recommend(catalogue, account, adventure, run_goal, patron)` (spec #22 Seam 2).

Composes the hard filter (`restrictions`), the search (`formation_search`) and the scores
(`champion_models`) into what the Formation view shows. Fields later tickets fill (familiars, modron,
hints, comparisons, Push Verdict) are present and empty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from advisor.account import Account
from advisor.catalogue import Adventure, Catalogue, Champion, Feat, Patron, Restriction, SpecializationOption
from advisor.champion_models import ChampionModels, Contribution, ModelStatus, Seat
from advisor.formation_search import EXACT_TIME_LIMIT_S, Candidate, SearchProblem, exact_search, exhaustive_search, search
from advisor.push_verdict import PushVerdict, push_verdict
from advisor.restrictions import describe, failed

RUN_GOAL_METRICS = {"Push": "Main DPS damage", "Gem Farm": "Gems per hour", "Gold Farm": "Favor per hour"}


@dataclass(frozen=True)
class MainDps:
    champion: Champion
    slot: int
    bud: float  # biggest hit, in the model's units


@dataclass(frozen=True)
class SeatRecommendation:
    slot: int
    champion: Champion | None = None  # None: empty or escort-blocked
    blocked: bool = False  # taken by an escort
    level: int | None = None
    is_main_dps: bool = False
    contribution: Contribution | None = None
    contribution_label: str = ""  # e.g. "+40% to Strix"
    specializations: tuple[SpecializationOption, ...] = ()
    feats: tuple[Feat, ...] = ()
    model_status: ModelStatus | None = None
    provided_by_adventure: bool = False  # a forced champion the player doesn't own
    familiar_id: int | None = None  # the familiar placed on this slot


@dataclass(frozen=True)
class FamiliarPlacement:
    familiar_id: int
    slot: int | None  # None: on clicks
    champion: Champion | None = None  # who it sits on

    @property
    def name(self) -> str:
        return f"Familiar {self.familiar_id}"


@dataclass(frozen=True)
class ModronSetup:
    core_id: int
    experience: int
    node_layout: tuple[tuple[int, ...], ...]  # the saved grid, trimmed to its placed nodes (0 = empty)
    reset_area: int | None  # None: no reset (or none saved)
    reset_area_optimised: bool  # chosen by the Run Goal's score (Gem Farm), else the saved area goal

    @property
    def name(self) -> str:
        return f"Core {self.core_id}"

    @property
    def nodes(self) -> int:
        return sum(1 for row in self.node_layout for tile in row if tile)


@dataclass(frozen=True)
class LeftOut:
    champion: Champion
    reason: str


@dataclass(frozen=True)
class FormationRecommendation:
    adventure: Adventure
    run_goal: str
    patron: Patron | None
    main_dps: MainDps | None
    seats: tuple[SeatRecommendation, ...]  # one per layout slot, in slot order
    score: float | None  # the Run Goal's score (relative units, see `score_label`)
    score_label: str
    gap_pct: float | None  # at most this % from the best possible
    exact: bool
    scoring_assumptions: tuple[str, ...]
    unverified_champions: tuple[Champion, ...]
    left_out: tuple[LeftOut, ...]
    infeasible_reason: str | None = None
    familiars: tuple[FamiliarPlacement, ...] = ()  # every owned familiar: on a slot or on clicks
    modron: ModronSetup | None = None
    # Filled by later tickets.
    hints: tuple = ()
    comparisons: tuple = ()
    push_verdict: PushVerdict | None = None  # Push only
    # The search this recommendation came from (candidates after restrictions), for one-change hints (#34).
    # Derived search state: not persisted (see results_codec), rebuilt by `with_search_problem`.
    problem: SearchProblem | None = field(default=None, compare=False, repr=False, metadata={"persist": False})
    # "Prove it's the best" (#31): None until run; "improved" (a better formation replaced the fast
    # one), "proven" (the shown formation is the best possible) or "timed_out" (gap_pct remains).
    exact_solve: str | None = None


def recommend(
    catalogue: Catalogue,
    account: Account,
    adventure: Adventure,
    run_goal: str,
    patron: int | None = None,
    *,
    models: ChampionModels | None = None,
    solver: str = "fast",  # "fast" (assignment + local search), "exact" (fast, then CP-SAT) or "exhaustive" (tiny instances only)
    budget_s: float = 0.1,
    exact_time_s: float = EXACT_TIME_LIMIT_S,
) -> FormationRecommendation:
    models = models or ChampionModels(catalogue, account)
    patron_obj = catalogue.patron(patron) if patron is not None else None
    campaign = catalogue.campaign(adventure.campaign_id)
    label = (
        f"{campaign.favor_name or 'Favor'} per hour (relative)" if run_goal == "Gold Farm"
        else "Gems per hour" if run_goal == "Gem Farm"
        else f"{RUN_GOAL_METRICS.get(run_goal, 'Score')} (relative)"
    )

    def infeasible(reason: str, left_out=()) -> FormationRecommendation:
        verdict = push_verdict(catalogue, account, models, adventure, patron, (), None) if run_goal == "Push" else None
        return FormationRecommendation(
            adventure, run_goal, patron_obj, None, _empty_seats(adventure), None, label, None, False,
            models.assumptions(run_goal), (), tuple(left_out), infeasible_reason=reason, push_verdict=verdict,
        )

    if not adventure.layout:
        return infeasible("No formation layout for this adventure in the Game Definitions.")
    usable = tuple(s for s in adventure.layout if s.index not in adventure.blocked_slots)
    if not usable:
        return infeasible("Every slot is taken by an escort.")

    names = {c.id: c.name for c in catalogue.champions}
    rules = [(r, None) for r in adventure.restrictions]
    rules += [(r, patron_obj.name) for r in patron_obj.restrictions] if patron_obj else []
    forced = {f.champion_id: f for f in adventure.forced if catalogue.find(f.champion_id)}
    candidates, left_out, rejected_by = _candidates(catalogue, account, adventure, patron_obj, models)
    if not candidates:
        return infeasible(_missing_piece(rejected_by, rules, names), left_out)
    by_id = {c.champion_id: c for c in candidates}
    problem = _search_problem(catalogue, account, usable, candidates)

    def scorer_for(main):
        return models.scorer(adventure.layout, run_goal, main, adventure=adventure)

    result = (exhaustive_search if solver == "exhaustive" else _fast(budget_s))(problem, scorer_for)
    exact_solve = None
    if solver == "exact" and result is not None:
        fast = result
        result = exact_search(problem, scorer_for, exact_time_s, incumbent=fast)
        exact_solve = "improved" if result.exact else "proven" if result.gap_pct == 0 else "timed_out"
    if result is None:
        return infeasible("No legal formation: the champions this adventure forces in don't fit its free slots.", left_out)

    main = catalogue.champion(result.main_dps.champion_id)
    placed = {s.slot: s for s in result.seats}
    setup = result.setup
    familiar_on = {
        slot: familiar for familiar, slot in zip(problem.familiars, setup.familiars if setup else ()) if slot is not None
    }
    seats = []
    for slot in adventure.layout:
        if slot.index in adventure.blocked_slots:
            seats.append(SeatRecommendation(slot.index, blocked=True))
            continue
        seat = placed.get(slot.index)
        if seat is None:
            seats.append(SeatRecommendation(slot.index))
            continue
        champion = catalogue.champion(seat.champion_id)
        owned = account.champions.get(champion.id)
        contribution = result.score.contributions.get(champion.id, Contribution())
        is_main = seat.champion_id == main.id
        seats.append(
            SeatRecommendation(
                slot=slot.index,
                champion=champion,
                level=owned.level if owned else (forced[champion.id].start_level if champion.id in forced else None),
                is_main_dps=is_main,
                contribution=contribution,
                contribution_label="Main DPS" if is_main else _label(contribution, main, run_goal),
                specializations=_specializations(catalogue, champion.id, seat.loadout.specializations),
                feats=tuple(catalogue.feats.get(f) or Feat(f, champion.id, f"Feat {f}", "") for f in seat.loadout.feats),
                model_status=models.status(champion.id),
                provided_by_adventure=owned is None,
                familiar_id=familiar_on.get(slot.index),
            )
        )
    familiars = tuple(
        FamiliarPlacement(f, slot, catalogue.champion(placed[slot].champion_id) if slot is not None else None)
        for f, slot in zip(problem.familiars, setup.familiars if setup else (None,) * len(problem.familiars))
    )
    modron = _modron(account, setup.core if setup else None, run_goal, result.score.reset_area)

    seated = {s.champion_id for s in result.seats}
    holder = {catalogue.champion(s.champion_id).seat: s for s in result.seats}
    scorer = scorer_for(result.main_dps)
    for c in candidates:
        if c.champion_id in seated:
            continue
        champion = catalogue.champion(c.champion_id)
        if champion.seat in holder:
            taken = holder[champion.seat]
            mine = max(scorer.bound(Seat(c.champion_id, taken.slot, l)) for l in c.loadouts)
            name = catalogue.champion(taken.champion_id).name
            if by_id[taken.champion_id].forced:
                reason = f"seat {champion.seat} taken by {name}, whom the adventure puts in the formation"
            elif scorer.bound(taken) > mine + 1e-9 or taken == result.main_dps:
                reason = f"seat {champion.seat} taken by {name}, a better pick"
            elif _level(account, taken.champion_id) > _level(account, c.champion_id):
                reason = f"seat {champion.seat} taken by {name} (same score, higher level)"
            else:
                reason = f"seat {champion.seat} taken by {name} (same score)"
        elif len(placed) >= len(usable):
            reason = "no free slot: every slot holds a pick at least as good"
        else:
            reason = "adds nothing to this formation"
        left_out.append(LeftOut(champion, reason))

    unverified = tuple(
        catalogue.champion(i) for i in sorted(seated, key=lambda i: catalogue.champion(i).seat)
        if models.status(i) is not ModelStatus.VERIFIED
    )
    verdict = (
        push_verdict(catalogue, account, models, adventure, patron, result.seats, result.main_dps)
        if run_goal == "Push" else None
    )
    return FormationRecommendation(
        adventure=adventure,
        run_goal=run_goal,
        patron=patron_obj,
        main_dps=MainDps(main, result.main_dps.slot, result.score.bud),
        seats=tuple(seats),
        score=math.exp(min(result.score.value, 700)),
        score_label=label,
        gap_pct=result.gap_pct,
        exact=result.exact or exact_solve == "proven",
        scoring_assumptions=models.assumptions(run_goal) + (verdict.assumptions if verdict else ()),
        unverified_champions=unverified,
        left_out=tuple(sorted(left_out, key=lambda l: (l.champion.seat, l.champion.id))),
        familiars=familiars,
        modron=modron,
        problem=problem,
        exact_solve=exact_solve,
        push_verdict=verdict,
    )


def with_search_problem(
    catalogue: Catalogue, account: Account, rec: FormationRecommendation, *, models: ChampionModels | None = None
) -> FormationRecommendation:
    """`rec` with the search it came from, rebuilt when missing (results read back from the Store
    don't persist it): the same candidates `recommend` searched, without searching again."""
    if rec.problem is not None or rec.main_dps is None:
        return rec
    adventure = rec.adventure
    usable = tuple(s for s in adventure.layout if s.index not in adventure.blocked_slots)
    candidates, _, _ = _candidates(catalogue, account, adventure, rec.patron, models or ChampionModels(catalogue, account))
    if not usable or not candidates:
        return rec
    return replace(rec, problem=_search_problem(catalogue, account, usable, candidates))


def _candidates(
    catalogue: Catalogue, account: Account, adventure: Adventure, patron_obj: Patron | None, models: ChampionModels
) -> tuple[list[Candidate], list[LeftOut], dict[Restriction, int]]:
    """The champions the search may seat (higher levels first), those the restrictions leave out,
    and how many each restriction rejected."""
    names = {c.id: c.name for c in catalogue.champions}
    rules = [(r, None) for r in adventure.restrictions]
    rules += [(r, patron_obj.name) for r in patron_obj.restrictions] if patron_obj else []
    forced = {f.champion_id: f for f in adventure.forced if catalogue.find(f.champion_id)}
    candidates, left_out, rejected_by = [], [], {}
    for champion in catalogue.champions:
        if champion.id in forced:
            f = forced[champion.id]
            slot = f.slot if f.slot is not None and f.slot not in adventure.blocked_slots else None
            candidates.append(Candidate(champion.id, champion.seat, models.loadouts(champion.id), True, slot))
            continue
        if not account.owns(champion.id):
            continue
        broken = [(r, who) for r, who in rules if failed([r], champion, account.server_time)]
        if broken:
            rule, who = broken[0]
            left_out.append(LeftOut(champion, f"not allowed{f' by {who}' if who else ''}: {describe(rule, names)}"))
            for r, _ in broken:
                rejected_by[r] = rejected_by.get(r, 0) + 1
            continue
        candidates.append(Candidate(champion.id, champion.seat, models.loadouts(champion.id)))
    # The search keeps the first of equally scored options: list higher levels first.
    candidates.sort(key=lambda c: -_level(account, c.champion_id))
    return candidates, left_out, rejected_by


def _search_problem(catalogue: Catalogue, account: Account, usable: tuple, candidates: list[Candidate]) -> SearchProblem:
    dps = frozenset(c.champion_id for c in candidates if "dps" in catalogue.champion(c.champion_id).tags)
    # Familiars are alike (their definitions are cosmetic); cores: most experience first.
    cores = tuple(sorted(account.modron_cores, key=lambda c: (-account.modron_cores[c], c)))
    return SearchProblem(
        usable, tuple(candidates), dps or frozenset(c.champion_id for c in candidates),
        tuple(sorted(account.familiar_ids)), cores,
    )


def _modron(account: Account, core_id: int | None, run_goal: str, reset_area: int | None) -> ModronSetup | None:
    if core_id is None:
        return None
    save = account.modron_saves.get(core_id)
    grid = save.grid if save else ()
    rows = [i for i, row in enumerate(grid) if any(row)]
    cols = [j for row in grid for j, tile in enumerate(row) if tile]
    trimmed = tuple(row[min(cols) : max(cols) + 1] for row in grid[min(rows) : max(rows) + 1]) if rows else ()
    if run_goal == "Gem Farm":
        area, optimised = reset_area, True
    elif run_goal == "Push":
        area, optimised = None, False  # a Push runs until it stalls
    else:
        area, optimised = save.area_goal if save else None, False
    return ModronSetup(core_id, account.modron_cores.get(core_id, 0), trimmed, area, optimised)


def _level(account: Account, champion_id: int) -> int:
    owned = account.champions.get(champion_id)
    return owned.level if owned else 0


def _fast(budget_s: float):
    return lambda problem, scorer_for: search(problem, scorer_for, budget_s)


def _empty_seats(adventure: Adventure) -> tuple[SeatRecommendation, ...]:
    return tuple(SeatRecommendation(s.index, blocked=s.index in adventure.blocked_slots) for s in adventure.layout)


def _label(contribution: Contribution, main: Champion, run_goal: str) -> str:
    label = f"+{contribution.damage_pct:.0f}% to {main.name}"
    if run_goal == "Gold Farm" and contribution.gold_pct >= 0.5:
        label += f" · +{contribution.gold_pct:.0f}% gold"
    return label


def _specializations(catalogue: Catalogue, champion_id: int, chosen: tuple[int, ...]) -> tuple[SpecializationOption, ...]:
    options = {o.id: o for c in catalogue.specializations.get(champion_id, ()) for o in c.options}
    return tuple(options[i] for i in chosen if i in options)


def _missing_piece(rejected_by: dict[Restriction, int], rules, names) -> str:
    """Names the rule that keeps the most owned champions out."""
    if not rejected_by:
        return "No legal formation: you own no champions."
    rule = max(rejected_by, key=lambda r: rejected_by[r])
    who = next((w for r, w in rules if r == rule), None)
    return (
        f"No legal formation: needs at least 1 champion that fits {f'{who}' + chr(39) + 's rule ' if who else ''}"
        f"\u201c{describe(rule, names)}\u201d, and none of yours do."
    )
