"""Formation Search: the best placement of candidates in a layout, around one Main DPS.

Knows nothing champion-specific (#5, #11): it sees candidates (a champion id, its seat number, the
loadouts it may use, whether the adventure forces it in) and asks a `Scorer` per Main DPS seat for
scores and per-seat bounds. Restrictions are applied before the search (only legal candidates are
passed in).

`search` fixes the Main DPS and its slot, places everyone else with an assignment over seat numbers ×
slots (a min-cost flow: a seat number holds at most one champion, so each seat row takes its best
champion and loadout for that slot), then hill-climbs on the full score. The assignment of bounds is
an upper bound per (Main DPS, slot); the best of them minus the best found is the reported gap.
`exhaustive_search` tries every legal formation and is only for tiny instances (the test oracle).

Familiars and modron cores (the `Setup`) are placed after the champions: a setup only lifts the
placement score (`Scorer.lift`, increasing), so the best placement stays best under every setup. The
fast search tries each split of familiars between the field (on the Main DPS, then the highest-bound
seats) and clicks, for each core; the exhaustive search tries every familiar on every seated slot or on
clicks. The upper bound is the placement bound lifted by the best setup.

`exact_search` is the on-demand exact CP-SAT solve (#31), time-limited and never worse than the
fast result it is given.
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from advisor.catalogue import Slot
from advisor.champion_models import FormationScore, Loadout, Seat


class Scorer(Protocol):
    def score(self, seats: Sequence[Seat], setup: Setup | None = None) -> FormationScore: ...

    def bound(self, seat: Seat) -> float: ...

    def lift(self, value: float, setup: Setup) -> float: ...


@dataclass(frozen=True)
class Candidate:
    champion_id: int
    seat: int  # the champion's seat number: a formation holds at most one champion per seat number
    loadouts: tuple[Loadout, ...] = (Loadout(),)
    forced: bool = False  # the adventure puts this champion in the formation
    forced_slot: int | None = None


@dataclass(frozen=True)
class SearchProblem:
    slots: tuple[Slot, ...]  # usable slots (escort-blocked slots already removed)
    candidates: tuple[Candidate, ...]
    main_dps_ids: frozenset[int]  # candidates allowed to be the Main DPS
    familiars: tuple[int, ...] = ()  # familiar ids to place: on a seated champion's slot, or on clicks
    cores: tuple[int, ...] = ()  # modron core ids to choose between, preferred first


@dataclass(frozen=True)
class Setup:
    """Where each familiar goes (a slot, or None for clicks; in `SearchProblem.familiars` order) and the
    modron core."""

    familiars: tuple[int | None, ...] = ()
    core: int | None = None


@dataclass(frozen=True)
class SearchResult:
    main_dps: Seat
    seats: tuple[Seat, ...]  # every seated champion, the Main DPS included
    score: FormationScore
    upper_bound: float  # log; no legal formation scores above it
    gap_pct: float  # at most this % below the best possible
    exact: bool  # from an exact solve (exhaustive here; CP-SAT in #31), not the fast search
    setup: Setup | None = None  # familiars and modron core, when the problem has any
    # Before the setup lifted them (`exact_search` works in these units); None: no setup applied.
    placement: FormationScore | None = None
    placement_upper_bound: float | None = None


ScorerFor = Callable[[Seat], Scorer]

_FILL = 1e-6  # tie-break: a free slot is filled when it costs nothing
_FORCE = 1e6  # a forced champion is always placed


def search(problem: SearchProblem, scorer_for: ScorerFor, budget_s: float = 0.1) -> SearchResult | None:
    started = time.perf_counter()
    plans = []
    for main in _main_seats(problem):
        scorer = scorer_for(main)
        plan = _assign(problem, main, scorer)
        if plan is not None:
            plans.append((plan[0], main, scorer, plan[1]))
    if not plans:
        return None
    plans.sort(key=lambda p: -p[0])  # most promising (Main DPS, slot) first
    best: tuple[FormationScore, Seat, tuple[Seat, ...]] | None = None
    for explored, (bound, main, scorer, seats) in enumerate(plans):
        if best is not None and bound <= best[0].value + 1e-12:
            break  # neither this nor any later (Main DPS, slot) can beat what was found
        if best is not None and explored >= 3 and time.perf_counter() - started > budget_s:
            break
        seats, score = _improve(problem, main, scorer, seats, deadline=started + budget_s)
        if best is None or score.value > best[0].value + 1e-12:
            best = (score, main, seats)
    # Local search proves nothing, so the highest assignment bound stands as the upper bound.
    upper_bound = max(plans[0][0], best[0].value)
    gap = _gap_pct(upper_bound, best[0].value)
    return _add_setup(problem, scorer_for, SearchResult(best[1], best[2], best[0], upper_bound, gap, exact=False))


def _add_setup(problem: SearchProblem, scorer_for: ScorerFor, result: SearchResult) -> SearchResult:
    """Places familiars and picks the core for a placement-level result, lifting its score and bound."""
    if not _has_setup(problem):
        return result
    scorer = scorer_for(result.main_dps)
    setup, score = max(
        ((s, scorer.score(result.seats, s)) for s in _quick_setups(problem, result.main_dps, result.seats, scorer)),
        key=lambda pair: pair[1].value,  # max keeps the first of equals
    )
    upper = max(score.value, max(scorer.lift(result.upper_bound, s) for s in _lift_setups(problem)))
    gap = 0.0 if result.gap_pct == 0 else _gap_pct(upper, score.value)
    return replace(
        result, score=score, upper_bound=upper, gap_pct=gap, setup=setup,
        placement=result.score, placement_upper_bound=result.upper_bound,
    )


def exhaustive_search(problem: SearchProblem, scorer_for: ScorerFor) -> SearchResult | None:
    """Every legal formation, scored in full. Exponential: tiny instances only."""
    best = None
    for main in _main_seats(problem):
        scorer = scorer_for(main)
        for seats in _all_formations(problem, main):
            if not _has_setup(problem):
                score = scorer.score(seats)
                if best is None or score.value > best[0].value + 1e-12:
                    best = (score, main, seats, None)
                continue
            # Skip the setups when even the best of them can't beat what was found (lift is increasing
            # and ignores which slots hold the familiars, so this only skips losers).
            placement = scorer.score(seats).value
            if best is not None and max(scorer.lift(placement, s) for s in _lift_setups(problem)) <= best[0].value + 1e-12:
                continue
            for setup in _all_setups(problem, seats):
                score = scorer.score(seats, setup)
                if best is None or score.value > best[0].value + 1e-12:
                    best = (score, main, seats, setup)
    if best is None:
        return None
    return SearchResult(best[1], best[2], best[0], best[0].value, 0.0, exact=True, setup=best[3])


def _has_setup(problem: SearchProblem) -> bool:
    return bool(problem.familiars or problem.cores)


def _cores(problem: SearchProblem):
    return problem.cores or (None,)


def _quick_setups(problem: SearchProblem, main: Seat, seats: tuple[Seat, ...], scorer: Scorer):
    """Familiars on the Main DPS, then on the seats with the highest bound; the rest on clicks."""
    order = sorted(seats, key=lambda s: (s != main, -scorer.bound(s), s.slot))
    count = len(problem.familiars)
    for core in _cores(problem):
        for k in range(min(count, len(order)), -1, -1):
            yield Setup(tuple(s.slot for s in order[:k]) + (None,) * (count - k), core)


def _lift_setups(problem: SearchProblem):
    """One setup per (field count, core): `lift` must not depend on which slots hold the familiars."""
    count = len(problem.familiars)
    for core in _cores(problem):
        for k in range(count + 1):
            yield Setup(tuple(range(k)) + (None,) * (count - k), core)


def _all_setups(problem: SearchProblem, seats: tuple[Seat, ...]):
    """Every familiar on every seated slot (one per slot) or on clicks, for every core."""
    targets = [s.slot for s in seats] + [None]
    for core in _cores(problem):
        for familiars in itertools.product(targets, repeat=len(problem.familiars)):
            placed = [f for f in familiars if f is not None]
            if len(placed) == len(set(placed)):
                yield Setup(familiars, core)


# Exact solve (#31): log weights are scaled to integers for CP-SAT. Each seat's weight is rounded,
# so the bound read back from the solver gets `_ROUNDING` per slot of slack to stay an upper bound,
# and two scores closer than `_PROVEN_TOL` count as equal (≈0.001%).
_SCALE = 1e6
_FILL_UNITS = 1000  # integer weight = bound units × this + 1: fills a free slot only on a tie
_ROUNDING = 0.5 / _SCALE
_PROVEN_TOL = 1e-5

EXACT_TIME_LIMIT_S = 60.0


def exact_search(
    problem: SearchProblem,
    scorer_for: ScorerFor,
    time_limit_s: float = EXACT_TIME_LIMIT_S,
    incumbent: SearchResult | None = None,
) -> SearchResult | None:
    """Provably best formation, within `time_limit_s` (CP-SAT per Main DPS seat).

    Main DPS seats are tried in descending order of their assignment bound and skipped once that
    bound can't beat the best found. Each one is a CP-SAT model maximising the sum of seat bounds;
    every solution is scored in full and cut off, until the solver's bound is no better than the
    best score. That is exact for any scorer whose `bound` honours its contract, separable or not.

    With an `incumbent` (the fast search's result), the answer never gets worse: a strictly better
    formation comes back with `exact=True`; otherwise the incumbent comes back with its gap
    tightened to what the solve proved (zero when it finished).
    """
    from ortools.sat.python import cp_model

    deadline = time.perf_counter() + max(0.0, time_limit_s)
    if incumbent is not None and incumbent.placement is not None:  # back to placement units
        incumbent = replace(
            incumbent, score=incumbent.placement, upper_bound=incumbent.placement_upper_bound,
            setup=None, placement=None, placement_upper_bound=None,
        )
    best = (incumbent.score, incumbent.main_dps, incumbent.seats) if incumbent else None
    found_here = False
    plans = []
    for main in _main_seats(problem):
        scorer = scorer_for(main)
        plan = _assign(problem, main, scorer)
        if plan is not None:
            plans.append((plan[0], main, scorer))
    plans.sort(key=lambda p: -p[0])
    open_bound = -math.inf  # highest bound over the work the time limit cut short
    for bound, main, scorer in plans:
        if best is not None and bound <= best[0].value + _PROVEN_TOL:
            break  # sorted: nothing left can beat the best found
        if time.perf_counter() >= deadline:
            open_bound = max(open_bound, bound)
            break
        base = scorer.score((main,)).value
        built = _exact_model(cp_model, problem, main, scorer)
        if built is None:
            continue  # a forced champion has no legal slot around this Main DPS seat
        model, choices = built
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                open_bound = max(open_bound, bound)
                break
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = remaining
            status = solver.Solve(model)
            if status == cp_model.INFEASIBLE or status == cp_model.MODEL_INVALID:
                break  # no formation left around this Main DPS seat
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                open_bound = max(open_bound, min(bound, _read_bound(solver, base, len(problem.slots))))
                break  # out of time before any solution
            chosen = [(x, seat) for x, seat in choices if solver.Value(x)]
            seats = (main, *(seat for _, seat in chosen))
            score = scorer.score(seats)
            if best is None or score.value > best[0].value + 1e-12:
                best, found_here = (score, main, seats), True
            reachable = min(bound, _read_bound(solver, base, len(problem.slots)))
            if reachable <= best[0].value + _PROVEN_TOL:
                break  # nothing around this seat beats the best found
            if status != cp_model.OPTIMAL:
                open_bound = max(open_bound, reachable)
                break  # out of time
            # This exact formation is scored: cut it off and look for the next best by bounds.
            model.AddBoolOr([x.Not() if solver.Value(x) else x for x, _ in choices])
    if best is None:
        return None
    ceiling = max(best[0].value, open_bound)
    if incumbent is not None:
        ceiling = max(best[0].value, min(ceiling, incumbent.upper_bound))
    gap = 0.0 if ceiling - best[0].value <= _PROVEN_TOL else _gap_pct(ceiling, best[0].value)
    if incumbent is not None and not found_here:
        return _add_setup(problem, scorer_for, replace(incumbent, upper_bound=ceiling, gap_pct=gap))
    return _add_setup(problem, scorer_for, SearchResult(best[1], best[2], best[0], ceiling, gap, exact=True))


def _exact_model(cp_model, problem: SearchProblem, main: Seat, scorer: Scorer):
    """CP-SAT model around a fixed Main DPS seat: one boolean per (champion, slot, loadout)."""
    model = cp_model.CpModel()
    usable = [s.index for s in problem.slots if s.index != main.slot]
    choices = []
    by_slot: dict[int, list] = {}
    by_seat_number: dict[int, list] = {}
    objective = []
    for c in _others(problem, main):
        slots = [c.forced_slot] if c.forced_slot is not None else usable
        mine = []
        for slot in slots:
            if slot not in usable:
                continue
            for loadout in c.loadouts:
                seat = Seat(c.champion_id, slot, loadout)
                x = model.NewBoolVar(f"c{c.champion_id}_s{slot}_{len(choices)}")
                choices.append((x, seat))
                mine.append(x)
                by_slot.setdefault(slot, []).append(x)
                by_seat_number.setdefault(c.seat, []).append(x)
                objective.append(x * (round(scorer.bound(seat) * _SCALE) * _FILL_UNITS + 1))
        if c.forced:
            if not mine:
                return None
            model.AddExactlyOne(mine)
    for group in (*by_slot.values(), *by_seat_number.values()):
        model.AddAtMostOne(group)
    model.Maximize(sum(objective))
    return model, choices


def _read_bound(solver, base: float, slots: int) -> float:
    """The solver's proven objective bound, back in log units (rounded up to stay an upper bound)."""
    return base + math.floor(solver.BestObjectiveBound()) / _FILL_UNITS / _SCALE + slots * _ROUNDING


def _gap_pct(upper_bound: float, found: float) -> float:
    excess = upper_bound - found
    return 0.0 if excess <= 1e-9 else (math.exp(min(excess, 700)) - 1) * 100


def _main_seats(problem: SearchProblem):
    usable = {s.index for s in problem.slots}
    claimed = {c.forced_slot: c.champion_id for c in problem.candidates if c.forced_slot is not None}
    for candidate in problem.candidates:
        if candidate.champion_id not in problem.main_dps_ids:
            continue
        slots = [candidate.forced_slot] if candidate.forced_slot is not None else sorted(usable)
        for slot in slots:
            if slot in usable and claimed.get(slot, candidate.champion_id) == candidate.champion_id:
                for loadout in candidate.loadouts:
                    yield Seat(candidate.champion_id, slot, loadout)


def _others(problem: SearchProblem, main: Seat):
    main_seat = next(c.seat for c in problem.candidates if c.champion_id == main.champion_id)
    return [c for c in problem.candidates if c.champion_id != main.champion_id and c.seat != main_seat]


def _assign(problem: SearchProblem, main: Seat, scorer: Scorer) -> tuple[float, tuple[Seat, ...]] | None:
    """Best placement by bounds around a fixed Main DPS seat: (upper bound, seats)."""
    others = _others(problem, main)
    fixed: list[Seat] = []
    for c in others:
        if c.forced_slot is not None:
            if c.forced_slot == main.slot:
                return None
            fixed.append(max((Seat(c.champion_id, c.forced_slot, l) for l in c.loadouts), key=scorer.bound))
    fixed_seats = {c.seat for c in others if c.forced_slot is not None}
    free = [s.index for s in problem.slots if s.index != main.slot and s.index not in {f.slot for f in fixed}]
    rows: dict[int, list[Candidate]] = {}
    for c in others:
        if c.forced_slot is None and c.seat not in fixed_seats:
            rows.setdefault(c.seat, []).append(c)
    # A forced champion leaves no choice in its seat row.
    for seat, members in rows.items():
        forced = [c for c in members if c.forced]
        if forced:
            rows[seat] = forced[:1]
    row_list = list(rows.values())
    if sum(1 for members in row_list if members[0].forced) > len(free):
        return None
    best_seat: dict[tuple[int, int], tuple[float, Seat]] = {}
    for r, members in enumerate(row_list):
        for slot in free:
            options = [Seat(c.champion_id, slot, l) for c in members for l in c.loadouts]
            seat = max(options, key=scorer.bound)
            best_seat[r, slot] = (scorer.bound(seat), seat)
    weights = [
        [best_seat[r, slot][0] + _FILL + (_FORCE if members[0].forced else 0) for slot in free]
        + [0.0] * len(row_list)  # leaving a seat row out of the formation
        for r, members in enumerate(row_list)
    ]
    assignment = _max_assignment(weights) if row_list else []
    seats = list(fixed)
    for r, column in enumerate(assignment):
        if column < len(free):
            seats.append(best_seat[r, free[column]][1])
        elif row_list[r][0].forced:
            return None
    base = scorer.score((main,)).value
    bound = base + sum(scorer.bound(s) for s in seats)
    return bound, (main, *seats)


def _improve(problem: SearchProblem, main: Seat, scorer: Scorer, seats: tuple[Seat, ...], deadline: float):
    """Hill-climb on the full score: move, swap, replace or change loadout of one seat at a time."""
    current = tuple(seats)
    current_score = scorer.score(current)
    others = _others(problem, main)
    by_id = {c.champion_id: c for c in others}
    usable = [s.index for s in problem.slots]
    improved = True
    while improved and time.perf_counter() < deadline:
        improved = False
        for neighbour in _neighbours(current, main, by_id, others, usable):
            score = scorer.score(neighbour)
            if score.value > current_score.value + 1e-12:
                current, current_score, improved = neighbour, score, True
                break
    return current, current_score


def _neighbours(seats, main, by_id, others, usable):
    taken_slots = {s.slot for s in seats}
    taken_seat_numbers = {by_id[s.champion_id].seat for s in seats if s.champion_id in by_id}
    seated = {s.champion_id for s in seats}
    movable = [i for i, s in enumerate(seats) if s != main and by_id[s.champion_id].forced_slot is None]
    empty = [slot for slot in usable if slot not in taken_slots]
    for i in movable:
        seat = seats[i]
        for slot in empty:
            yield _replace(seats, i, Seat(seat.champion_id, slot, seat.loadout))
        for loadout in by_id[seat.champion_id].loadouts:
            if loadout != seat.loadout:
                yield _replace(seats, i, Seat(seat.champion_id, seat.slot, loadout))
        own_seat = by_id[seat.champion_id].seat
        if not by_id[seat.champion_id].forced:
            yield seats[:i] + seats[i + 1 :]
            for c in others:
                if c.champion_id not in seated and (c.seat == own_seat or c.seat not in taken_seat_numbers):
                    for loadout in c.loadouts:
                        yield _replace(seats, i, Seat(c.champion_id, seat.slot, loadout))
    for i, j in itertools.combinations(movable, 2):
        a, b = seats[i], seats[j]
        yield _replace(_replace(seats, i, Seat(a.champion_id, b.slot, a.loadout)), j, Seat(b.champion_id, a.slot, b.loadout))
    for slot in empty:
        for c in others:
            if c.champion_id not in seated and c.seat not in taken_seat_numbers and c.forced_slot is None:
                for loadout in c.loadouts:
                    yield (*seats, Seat(c.champion_id, slot, loadout))


def _replace(seats: tuple[Seat, ...], i: int, seat: Seat) -> tuple[Seat, ...]:
    return seats[:i] + (seat,) + seats[i + 1 :]


def _all_formations(problem: SearchProblem, main: Seat):
    others = _others(problem, main)
    fixed = [c for c in others if c.forced_slot is not None]
    if any(c.forced_slot == main.slot for c in fixed):
        return
    fixed_slots = {c.forced_slot for c in fixed}
    free = [s.index for s in problem.slots if s.index != main.slot and s.index not in fixed_slots]
    rows: dict[int, list[Candidate]] = {}
    for c in others:
        if c.forced_slot is None and c.seat not in {f.seat for f in fixed}:
            rows.setdefault(c.seat, []).append(c)
    row_list = list(rows.values())
    for fixed_loadouts in itertools.product(*[[Seat(c.champion_id, c.forced_slot, l) for l in c.loadouts] for c in fixed]):
        yield from _fill(row_list, free, [main, *fixed_loadouts])


def _fill(rows, free, placed):
    if not rows:
        yield tuple(placed)
        return
    members, rest = rows[0], rows[1:]
    if not any(c.forced for c in members):
        yield from _fill(rest, free, placed)  # this seat number stays out
    for c in members:
        if any(m.forced for m in members) and not c.forced:
            continue
        for slot in free:
            for loadout in c.loadouts:
                yield from _fill(rest, [s for s in free if s != slot], [*placed, Seat(c.champion_id, slot, loadout)])


def _max_assignment(weights: list[list[float]]) -> list[int]:
    """Column chosen per row maximising the total weight (Hungarian algorithm; rows ≤ columns)."""
    n, m = len(weights), len(weights[0])
    cost = [[-w for w in row] for row in weights]
    u, v = [0.0] * (n + 1), [0.0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [math.inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], math.inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    result = [0] * n
    for j in range(1, m + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


def plan_bounds(problem: SearchProblem, scorer_for: ScorerFor) -> dict[Seat, float]:
    """The assignment bound of every feasible Main DPS seat: no formation built around that seat
    scores above it (log). The cheap first step of `search`, used alone to prune one-change hints."""
    bounds = {}
    for main in _main_seats(problem):
        plan = _assign(problem, main, scorer_for(main))
        if plan is not None:
            bounds[main] = plan[0]
    return bounds


def lifted(problem: SearchProblem, scorer: Scorer, value: float) -> float:
    """A placement-level bound in the Run Goal's units: lifted by the best setup (familiars, cores)."""
    if not _has_setup(problem):
        return value
    return max(scorer.lift(value, s) for s in _lift_setups(problem))
