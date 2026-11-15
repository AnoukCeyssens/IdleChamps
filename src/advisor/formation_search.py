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
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
    score, main, seats = best
    setup = None
    if _has_setup(problem):
        scorer = scorer_for(main)
        setup, score = max(
            ((s, scorer.score(seats, s)) for s in _quick_setups(problem, main, seats, scorer)),
            key=lambda pair: pair[1].value,  # max keeps the first of equals
        )
        upper_bound = max(score.value, max(scorer.lift(upper_bound, s) for s in _lift_setups(problem)))
    gap = _gap_pct(upper_bound, score.value)
    return SearchResult(main, seats, score, upper_bound, gap, exact=False, setup=setup)


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
