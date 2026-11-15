"""The advisor's state for the web UI: everything the pages show, rebuilt from raw responses.

`Session.load` runs at start and after each Refresh: it parses the Catalogue and Account, merges
the Area Set Cache, runs validation (live damage checks, Model Status transitions, Push
calibration) and hands the new data to the Recompute and Next Steps. The other methods answer
what a page asks for; the web layer (`advisor.web`) only renders them.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace

from advisor import model_status
from advisor.account import Account, parse_account
from advisor.area_sets import merge as merge_area_sets
from advisor.catalogue import Adventure, Catalogue, Champion, parse_catalogue
from advisor.champion_models import ChampionModels, ModelStatus
from advisor.champion_usage import champion_steps, seated_in
from advisor.hints import RAIL_HINTS, Hint
from advisor.hints import hints as one_change_hints
from advisor.next_steps import NEXT_STEP_GOALS, FocusSets, NextStep, ReadyToComplete, Steps, measure_adventure, next_steps
from advisor.next_steps import ready_to_complete
from advisor.recommendation import FormationRecommendation, recommend, with_current_verdict, with_search_problem
from advisor.recompute import Recompute
from advisor.request_log import RawResponses
from advisor.store import Store

_log = logging.getLogger(__name__)

# Run Goals whose tab shows a Formation Recommendation (all precomputed). Push with its Push Verdict (#32).
RECOMMENDED_GOALS = ("Gem Farm", "Gold Farm", "Push")


@dataclass(frozen=True)
class RankedSteps:
    steps: tuple[NextStep, ...]  # ranked, best first; empty while any Focus adventure is still measuring
    waiting: int  # Focus adventures still measuring in the background
    focus_count: int


@dataclass(frozen=True)
class StatusRow:
    champion: Champion
    status: ModelStatus


@dataclass(frozen=True)
class ModelStatusOverview:
    """The Model Status page's content."""

    table: tuple[StatusRow, ...]  # catalogue order
    flags: tuple[model_status.ReviewFlag, ...]  # open model review flags
    calibration: tuple[model_status.CalibrationFlag, ...]
    checks: tuple[model_status.DamageCheck, ...]
    coverage: str  # "Monster placement known for N of M adventures (K of L area sets)"
    history: str  # "History: N records since <date>"


@dataclass(frozen=True)
class Evidence:
    text: str
    passed: bool | None  # None: neither (Not Modelled)


class Session:
    def __init__(
        self,
        *,
        results: Recompute | None = None,
        focus: FocusSets | None = None,
        steps: Steps | None = None,
        store: Store | None = None,
    ) -> None:
        """Offline (no `store`): Formation Recommendations and Next Steps computed on demand.
        Live: `results`/`steps` precompute in the background, and the Store keeps the Area Set
        Cache, the Model Status ledger and the History."""
        self.store = store
        self.results = results or Recompute(RECOMMENDED_GOALS)
        self.focus = focus or FocusSets()  # Next Steps' Focus Sets (#35)
        self.steps = steps or Steps()  # each Focus adventure's measured one-changes
        self.catalogue, self.account = Catalogue(champions=()), Account(champions={})
        self.models = ChampionModels(self.catalogue, self.account)
        self.ledger = model_status.from_json(store.load_model_status()) if store is not None else model_status.Ledger()
        self.checks: tuple[model_status.DamageCheck, ...] = ()  # the live damage checks on the shown snapshot
        self.calibration: tuple[model_status.CalibrationFlag, ...] = ()  # Push calibration flags on it (#39)
        self._lock = threading.Lock()
        self._hints: dict[tuple, tuple[Hint, ...]] = {}  # rail hints computed when a recommendation was opened
        self._verdicts: dict[tuple, tuple[FormationRecommendation, FormationRecommendation]] = {}

    # --- loading ----------------------------------------------------------------------------

    def load(self, responses: RawResponses, *, refreshed: bool = True) -> None:
        """(Re)build everything the pages show from raw responses (at start and after a Refresh)."""
        if "getdefinitions" not in responses or "getuserdetails" not in responses:
            return  # live mode before the first Refresh: nothing to show yet
        catalogue = parse_catalogue(responses)
        if self.store is not None:
            cache = merge_area_sets(self.store.load_area_sets(), catalogue.area_sets)
            self.store.save_area_sets(cache)
            catalogue = replace(catalogue, area_sets=cache)
        account = parse_account(responses)
        checks = model_status.damage_checks(catalogue, account, ChampionModels(catalogue, account))
        ledger = model_status.advance(self.ledger, catalogue, account, checks)
        if self.store is not None:
            try:
                self.store.save_model_status(model_status.to_json(ledger))
            except OSError as error:  # kept in memory; the next Refresh saves it
                _log.warning("Model Status ledger not saved: %s", error)
        models = ChampionModels(catalogue, account, statuses=ledger.statuses())
        self.catalogue, self.account, self.models, self.ledger, self.checks = catalogue, account, models, ledger, checks
        self.calibration = model_status.push_calibration(catalogue, account, models)
        self.results.update(responses, catalogue, account, models, precompute=refreshed)
        with self._lock:
            self._hints.clear()
            self._verdicts.clear()
        self.steps.update(responses, account, catalogue, models)  # with the Area Set Cache and Model Statuses (#36)
        self.steps.ensure((a, g) for g in NEXT_STEP_GOALS for a in self.focus.get(catalogue, account, g))

    # --- Formation view ---------------------------------------------------------------------

    def recommendation(
        self, adventure: Adventure, goal: str, patron: int | None = None
    ) -> tuple[FormationRecommendation | None, bool]:
        """The shown recommendation (None for a Run Goal without one) and whether it is stale. Its
        Push Verdict is always made for the current snapshot's runs (`with_current_verdict`)."""
        if goal not in RECOMMENDED_GOALS:
            return None, False
        rec, stale = self.results.get(adventure, goal, patron)
        if goal != "Push":
            return rec, stale
        key = (adventure.id, goal, patron)
        with self._lock:
            seen = self._verdicts.get(key)
        if seen is not None and seen[0] is rec:
            return seen[1], stale
        current = with_current_verdict(self.catalogue, self.account, rec, models=self.models)
        with self._lock:
            self._verdicts[key] = (rec, current)
        return current, stale

    def rail_hints(self, adventure: Adventure, goal: str, patron: int | None) -> tuple[tuple[Hint, ...], str]:
        """"What would make this better" (the best RAIL_HINTS one-changes) and the metric they're in.
        A Focus adventure's background measurement is reused; otherwise computed now."""
        rec, stale = self.recommendation(adventure, goal, patron)
        if rec is None:
            return (), ""
        metric = rec.score_label.removesuffix(" (relative)")
        key = (adventure.id, goal, patron)
        with self._lock:
            if key in self._hints:
                return self._hints[key], metric
        measured = self.steps.get(adventure.id, goal) if patron is None and not stale else None
        if measured is not None:
            shown = measured[:RAIL_HINTS]
        else:
            rec = with_search_problem(self.catalogue, self.account, rec, models=self.models)  # persisted results carry none
            shown = one_change_hints(self.catalogue, self.account, rec, models=self.models)
        if not stale:  # a stale result is replaced once the recompute reaches it
            with self._lock:
                self._hints[key] = shown
        return shown, metric

    def prove(self, adventure: Adventure, goal: str, patron: int | None) -> None:
        """"Prove it's the best" (#31): the exact solve (~60 s) replaces the shown recommendation;
        it keeps the fast formation unless it finds a better one."""
        if goal not in RECOMMENDED_GOALS:
            return
        proven = recommend(self.catalogue, self.account, adventure, goal, patron, models=self.models, solver="exact")
        self.results.put(adventure, goal, patron, proven)
        with self._lock:
            self._hints.pop((adventure.id, goal, patron), None)  # gains are measured against the shown formation
        if patron is None:
            self.steps.drop(adventure.id, goal)

    # --- Next Steps -------------------------------------------------------------------------

    def focus_adventures(self, goal: str) -> list[Adventure]:
        return [a for i in self.focus.get(self.catalogue, self.account, goal) if (a := self.catalogue.adventure(i))]

    def is_focus(self, adventure: Adventure, goal: str) -> bool:
        return adventure.id in self.focus.get(self.catalogue, self.account, goal)

    def pin(self, adventure: Adventure, goal: str, pinned: bool) -> None:
        self.focus.pin(self.catalogue, self.account, goal, adventure.id, pinned)
        self.steps.ensure([(adventure.id, goal)] if pinned else [])

    def ranked_steps(self, goal: str) -> RankedSteps:
        """The Run Goal's ranked Next Steps. Measurements come from the background (live mode,
        over the Area Set Cache and Model Statuses); what's missing is measured now."""
        adventures = self.focus_adventures(goal)
        self.steps.ensure((a.id, goal) for a in adventures)
        measured, waiting = {}, 0
        for adventure in adventures:
            found = self.steps.get(adventure.id, goal)
            if found is None and self.steps.pending(adventure.id, goal):
                waiting += 1
                continue
            if found is None:
                rec, stale = self.results.get(adventure, goal)
                found = measure_adventure(self.catalogue, self.account, rec, models=self.models)
                if not stale:
                    self.steps.put(adventure.id, goal, found)
            measured[adventure.id] = found
        ranked = () if waiting else next_steps(self.catalogue, self.account, adventures, measured)
        return RankedSteps(ranked, waiting, len(adventures))

    def ready(self) -> tuple[ReadyToComplete, ...]:
        """Push's Ready to complete (#36), from the shown Push recommendations' verdicts."""
        return ready_to_complete(
            self.catalogue, self.account, lambda adventure, patron: self.recommendation(adventure, "Push", patron)[0].push_verdict
        )

    # --- champions --------------------------------------------------------------------------

    def champion_steps(self, champion_id: int, goal: str) -> tuple[tuple[NextStep, ...], int]:
        """A Run Goal's Next Steps involving the champion, and Focus adventures still measuring."""
        ranked = self.ranked_steps(goal)
        return champion_steps(champion_id, ranked.steps), ranked.waiting

    def champion_usage(self, champion_id: int) -> dict:
        """How the advisor uses a champion (#37): its explanation, where it is seated so far."""
        explanation = self.models.explain(champion_id)
        held = self.results.available()
        return {
            "explanation": explanation,
            "seated": seated_in(champion_id, held, explanation),
            "computed": sum(1 for rec, _ in held if rec.patron is None),
            "computable": len(self.catalogue.adventures) * len(RECOMMENDED_GOALS),
        }

    def evidence(self, champion_id: int) -> list[Evidence]:
        """What a champion's Model Status rests on, oldest first: description tests, then checks."""
        reconstruction = self.models.reconstruction(champion_id)
        if reconstruction is None:
            return [Evidence("Scored on its generic effects only: no reconstruction of its unique effects yet.", None)]
        tests = reconstruction.description_tests
        rows = [Evidence(
            f"Description tests: {len(tests)} ({', '.join(tests)}), kept passing by the test suite."
            if tests else "No description tests yet: it can't reach Verified.",
            bool(tests),
        )]
        record = self.ledger.records.get(champion_id)
        for e in record.evidence if record else ():
            rows.append(Evidence((f"{_date(e.at)}: " if e.at else "") + e.text, e.passed))
        return rows

    # --- Model Status page ------------------------------------------------------------------

    def model_status_overview(self) -> ModelStatusOverview:
        catalogue = self.catalogue
        with_sets = [a for a in catalogue.adventures if a.area_set_id is not None]
        known = [a for a in with_sets if a.area_set_id in catalogue.area_sets]
        coverage = (
            f"Monster placement known for {len(known)} of {len(with_sets)} adventures "
            f"({len({a.area_set_id for a in known})} of {len({a.area_set_id for a in with_sets})} area sets)"
        )
        if self.store is None:
            history = "History: not recorded in offline mode"
        else:
            kept = self.store.load_history()
            first = next((r.get("server_time") for r in kept if r.get("server_time")), None)
            history = f"History: {len(kept)} record{'' if len(kept) == 1 else 's'}" + (
                f" since {_date(first)}" if first else ""
            )
        fresh = [rec for rec, stale in self.results.available() if not stale]
        return ModelStatusOverview(
            table=tuple(StatusRow(c, self.models.status(c.id)) for c in catalogue.champions),
            flags=self.ledger.open_flags() + model_status.community_flags(fresh),
            calibration=self.calibration,
            checks=self.checks,
            coverage=coverage,
            history=history,
        )


def _date(server_time: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(server_time))
