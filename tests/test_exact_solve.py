""""Prove it's the best" (#31): the exact CP-SAT solve, on demand, ~60 s.

Seam 2 (`recommend(..., solver="exact")`) is checked against the exhaustive oracle on small
instances. Today's generic Champion Models are separable, so the fast search is already optimal on
them; the case where the exact solve beats the fast search needs seats that interact, which only a
synthetic scorer can show until per-champion models (#40) exist. Those tests go through
`formation_search.exact_search`, the champion-agnostic search interface.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from advisor.recommendation import recommend
from test_recommendation import DIAMOND, LINE, _small_roster, modelled, raw  # noqa: F401  (fixtures)


@pytest.mark.parametrize("run_goal", ["Gold Farm", "Push"])
@pytest.mark.parametrize(
    "adventure_id, layout, roster",
    [
        (7, DIAMOND, {12, 2, 1, 4, 5, 43}),
        (7, LINE, {12, 153, 2, 1, 4, 3, 43}),
        (2024, DIAMOND, {12, 153, 43, 73, 128, 9}),  # only Evil, Kitiara forced in
        (361, LINE, {2, 1, 5, 43, 12}),  # no DPS
        (360, DIAMOND, {2, 1, 3, 12, 4}),  # Turiel forced into slot 1
    ],
)
def test_exact_solve_agrees_with_exhaustive_search_and_proves_it(modelled, adventure_id, layout, roster, run_goal):
    catalogue, account = modelled
    account = _small_roster(account, roster)
    adventure = replace(catalogue.adventure(adventure_id), layout=layout, blocked_slots=frozenset())

    exhaustive = recommend(catalogue, account, adventure, run_goal, solver="exhaustive")
    exact = recommend(catalogue, account, adventure, run_goal, solver="exact")

    assert exact.score == pytest.approx(exhaustive.score, rel=1e-9)
    assert exact.gap_pct == 0
    assert exact.exact
    assert exact.exact_solve == "proven"


# --- seats that interact: a synthetic scorer (bound = most a seat ever adds) ------------------

from advisor.catalogue import Slot  # noqa: E402
from advisor.champion_models import FormationScore, Seat  # noqa: E402
from advisor.formation_search import Candidate, SearchProblem, exact_search, exhaustive_search, search  # noqa: E402

MAIN, A, B, C, D = 100, 1, 2, 3, 4
TRIANGLE = (Slot(0, 20, 10, 0, (1, 2)), Slot(1, 10, 0, 1, (0, 2)), Slot(2, 10, 20, 1, (0, 1)))


class PairScorer:
    """A and B are worth 10 together and nothing apart; C and D are worth 6 each.
    The fast search's assignment (by bounds) seats A and B, and no single move from there helps;
    C with D (12) is the best formation."""

    alone = {C: 6.0, D: 6.0}
    pairs = {frozenset((A, B)): 10.0}
    bounds = {A: 10.0, B: 10.0, C: 6.0, D: 6.0}

    def score(self, seats):
        ids = {s.champion_id for s in seats}
        value = sum(self.alone.get(i, 0.0) for i in ids)
        value += sum(v for pair, v in self.pairs.items() if pair <= ids)
        return FormationScore(value, 1.0)

    def bound(self, seat):
        return self.bounds.get(seat.champion_id, 0.0)


def _pair_problem():
    candidates = tuple(Candidate(i, seat=i) for i in (MAIN, A, B, C, D))
    return SearchProblem(TRIANGLE, candidates, frozenset({MAIN}))


def test_exact_solve_replaces_a_worse_fast_result_and_marks_it_exact():
    problem = _pair_problem()
    fast = search(problem, lambda main: PairScorer())
    assert fast.score.value == 10.0 and fast.gap_pct > 0  # stuck on A + B

    result = exact_search(problem, lambda main: PairScorer(), time_limit_s=10, incumbent=fast)

    assert result.exact
    assert result.score.value == 12.0
    assert {s.champion_id for s in result.seats} == {MAIN, C, D}
    assert result.gap_pct == 0


def test_exact_solve_with_interacting_seats_agrees_with_exhaustive_search():
    problem = _pair_problem()

    exact = exact_search(problem, lambda main: PairScorer(), time_limit_s=10)
    oracle = exhaustive_search(problem, lambda main: PairScorer())

    assert exact.score.value == oracle.score.value == 12.0
    assert exact.gap_pct == 0


def test_out_of_time_keeps_the_fast_result_and_its_remaining_gap():
    problem = _pair_problem()
    fast = search(problem, lambda main: PairScorer())

    result = exact_search(problem, lambda main: PairScorer(), time_limit_s=0, incumbent=fast)

    assert not result.exact
    assert result.seats == fast.seats
    assert result.gap_pct == fast.gap_pct > 0


def test_exact_solve_stops_at_its_time_limit():
    # Many seats that each pair up: lots of formations look good by bounds and must be scored.
    slots = tuple(Slot(i, 10 * i, 0, i, ()) for i in range(6))
    ids = list(range(1, 25))
    pairs = {frozenset((i, i + 1)): 10.0 for i in ids[::2]}

    class Many(PairScorer):
        alone = {i: 1.0 for i in ids}
        bounds = {i: 11.0 for i in ids}

    Many.pairs = pairs
    problem = SearchProblem(slots, tuple(Candidate(i, seat=i) for i in [MAIN, *ids]), frozenset({MAIN}))
    fast = search(problem, lambda main: Many())

    started = time.perf_counter()
    result = exact_search(problem, lambda main: Many(), time_limit_s=0.5, incumbent=fast)

    assert time.perf_counter() - started < 1.5
    assert result.score.value >= fast.score.value
    assert result.gap_pct > 0


# --- the button, through the HTTP app ----------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from advisor.web import create_app  # noqa: E402
from conftest import FIXTURE_LOG, rows  # noqa: E402

GOLD = "/adventures/{}?goal=Gold%20Farm"


@pytest.fixture
def app() -> TestClient:
    return TestClient(create_app(FIXTURE_LOG))  # its own recommendation cache


def _form(page):
    (form,) = rows(page, "prove")
    return form


def test_every_recommendation_offers_prove_its_the_best(app):
    for adventure_id in (7, 289, 2024):
        page = app.get(GOLD.format(adventure_id)).text
        form = _form(page)
        assert "Prove it's the best" in form["text"]
        assert "Solving, up to 60 s" in form["text"]  # the running state, shown while it works


def test_proving_shows_the_outcome_and_keeps_the_formation(app):
    before = app.get(GOLD.format(9) + "&patron=2").text

    after = app.post("/adventures/9/prove?goal=Gold%20Farm&patron=2").text

    (outcome,) = rows(after, "exact-outcome")
    assert outcome["text"] == "Proven best: exact solve found nothing better"
    assert [r["text"] for r in rows(after, "seat-row")] == [r["text"] for r in rows(before, "seat-row")]
    (gap,) = rows(after, "gap")
    assert gap["text"] == "at most 0% from best"
    assert rows(after, "prove") == []  # nothing left to prove
    # The proven result stays for this adventure, Run Goal and patron.
    assert rows(app.get(GOLD.format(9) + "&patron=2").text, "exact-outcome") == [outcome]
