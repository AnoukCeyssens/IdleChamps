"""Next Steps (#35; spec #22, #8, #15): one-change actions ranked per Run Goal across its Focus Set.

A Next Step is one change from the one-change engine (`hints.py`, potions left out), measured on
its own against the current snapshot on every adventure of the Run Goal's Focus Set, and ranked by
impact only: its average relative gain over the Focus Set (an adventure it doesn't help counts 0).
Cost is shown against what the player holds but never scored.

The Focus Set is the adventures a Run Goal's Next Steps are measured against. Until the player
pins or unpins one, it is the default for the snapshot:

- Gem Farm: the adventures of game instances that farm (their campaign has a modron core with
  reset on and a reset area set); with none, the last-played adventure.
- Gold Farm: per campaign whose Favor the player has earned, the adventures its instances are on;
  with none there, the adventure the player got furthest in.
- Push: per campaign the player is progressing, the furthest unfinished adventure they can start.

The Push tab opens with Ready to complete (`ready_to_complete`): objectives the Push Verdict now
calls Likely that the player hasn't completed and can start now.

Measuring a Gem Farm adventure takes seconds (every change is re-searched), so in live mode the
Focus Set's adventures are measured in a background pool after each Refresh that changes the
recommendation key (`Steps`), and on demand otherwise.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Executor, Future
from dataclasses import dataclass, replace

from advisor.account import RARITY_NAMES, Account, parse_account
from advisor.catalogue import Adventure, Catalogue, Patron, parse_catalogue
from advisor.champion_models import ChampionModels, ModelStatus
from advisor.explorer import is_done, is_running, is_unlocked, patron_completion, tree
from advisor.hints import CONTRACT_LEVELS, MIN_GAIN_PCT, MODRON_LEVEL_EXP, Change, Hint, candidate_changes, measure
from advisor.push_verdict import PushVerdict
from advisor.recompute import _area_sets_key, _statuses_key
from advisor.recommendation import FormationRecommendation, recommend, with_search_problem
from advisor.request_log import RawResponses
from advisor.store import Store

# Run Goals with a Next Steps tab (Push's opens with Ready to complete, #36).
NEXT_STEP_GOALS = ("Push", "Gem Farm", "Gold Farm")
# Steps shown before "show all".
TOP_STEPS = 10

# Inventory ids for the cost line (community values, unverified against Game Definitions: the
# recording has no `buff_defines` or `chest_type_defines`).
LARGE_CONTRACT_BUFF_ID = 34  # Large Blacksmithing Contract
GOLD_CHEST_ID = 2  # Gold Chest (Silver = 1)


# --- Focus Set ---------------------------------------------------------------------------------


def default_focus(catalogue: Catalogue, account: Account, goal: str) -> tuple[int, ...]:
    """The Focus Set before the player pins or unpins anything (adventure ids)."""
    known = {a.id: a for a in catalogue.adventures}
    if goal == "Gem Farm":
        farming = {c for s in account.modron_saves.values() if s.reset_enabled and s.area_goal for c in s.campaign_ids}
        ids = [
            i.adventure_id for i in account.instances
            if i.adventure_id in known and known[i.adventure_id].campaign_id in farming
        ]
        if not ids and account.last_played_adventure_id in known:
            ids = [account.last_played_adventure_id]
        return tuple(dict.fromkeys(ids))
    if goal == "Gold Farm":
        ids = []
        for campaign in catalogue.campaigns:
            if not account.favor_earned.get(campaign.favor_currency_id):
                continue
            mine = [a for a in catalogue.adventures if a.campaign_id == campaign.id]
            here = [i.adventure_id for i in account.instances if i.adventure_id in {a.id for a in mine}]
            if not here and mine:
                here = [max(mine, key=lambda a: account.best_areas.get(a.id, 0)).id]
            ids.extend(here)
        return tuple(dict.fromkeys(ids))
    if goal == "Push":
        return _default_push_focus(catalogue, account)
    return ()


def _default_push_focus(catalogue: Catalogue, account: Account) -> tuple[int, ...]:
    """Per campaign I'm progressing (one I can play with an adventure done or run), the furthest
    unfinished adventure I can start: the one with the highest best area, else the first in order."""
    ids = []
    for node in tree(catalogue, account):
        mine = [a for a in catalogue.adventures if a.campaign_id == node.campaign.id]
        if not node.running or not any(account.best_areas.get(a.id) or is_done(account, a) for a in mine):
            continue
        open_ = [
            a for a in mine
            if a.objective_area is not None and not is_done(account, a) and is_unlocked(catalogue, account, a)
        ]
        if open_:
            ids.append(max(open_, key=lambda a: account.best_areas.get(a.id, 0)).id)  # ties: the first
    return tuple(ids)


class FocusSets:
    """The pinned Focus Sets, persisted in the Store (`focus.json.gz`) when there is one. A Run
    Goal the player never edited follows `default_focus` for the current snapshot."""

    def __init__(self, store: Store | None = None) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._pinned: dict[str, tuple[int, ...]] = dict(store.load_focus() or {}) if store else {}

    def get(self, catalogue: Catalogue, account: Account, goal: str) -> tuple[int, ...]:
        with self._lock:
            pinned = self._pinned.get(goal)
        return pinned if pinned is not None else default_focus(catalogue, account, goal)

    def pin(self, catalogue: Catalogue, account: Account, goal: str, adventure_id: int, pinned: bool) -> None:
        with self._lock:
            current = self._pinned.get(goal)
            if current is None:
                current = default_focus(catalogue, account, goal)
            if pinned:
                current = tuple(dict.fromkeys((*current, adventure_id)))
            else:
                current = tuple(i for i in current if i != adventure_id)
            self._pinned[goal] = current
            saved = {g: list(ids) for g, ids in self._pinned.items()}
        if self._store is not None:
            try:
                self._store.save_focus(saved)
            except OSError:
                pass  # kept in memory


# --- Ready to complete (Push) ------------------------------------------------------------------


@dataclass(frozen=True)
class ReadyToComplete:
    adventure: Adventure
    patron: Patron | None  # a patron objective
    verdict: PushVerdict
    unlocks: tuple[str, ...]  # what completing it unlocks, e.g. "patron Mirt", "variant Skittering Skulls"


def ready_to_complete(
    catalogue: Catalogue, account: Account, verdict_for: Callable[[Adventure, int | None], PushVerdict | None]
) -> tuple[ReadyToComplete, ...]:
    """Objectives whose Push Verdict is now Likely and that I haven't completed, on adventures I
    can start now (an event's only while it runs). Patron objectives count for unlocked patrons; a
    patron completion the snapshot can't read counts as not completed (here only). Ordered by what
    completing unlocks (a patron, then a variant, then an event champion), then campaign order.
    `verdict_for(adventure, patron)` gives the shown Push recommendation's verdict."""
    order = {
        a.id: (i, j)
        for i, node in enumerate(tree(catalogue, account))
        for j, a in enumerate(a for a in catalogue.adventures if a.campaign_id == node.campaign.id)
    }
    found = []
    for adventure in catalogue.adventures:
        if adventure.id not in order or not is_unlocked(catalogue, account, adventure):
            continue
        objectives: list[int | None] = []
        if adventure.objective_area is not None and not is_done(account, adventure):
            objectives.append(None)
        objectives += [
            p for p in adventure.patron_objectives
            if p in account.unlocked_patron_ids and patron_completion(account, adventure, p) is not True
        ]
        for patron in objectives:
            verdict = verdict_for(adventure, patron)
            if verdict is None or verdict.kind != "likely":
                continue
            unlocks = _unlocks(catalogue, account, adventure) if patron is None else ()
            found.append(ReadyToComplete(adventure, catalogue.patron(patron) if patron else None, verdict, unlocks))

    def rank(entry: ReadyToComplete) -> tuple:
        kinds = {u.split(" ", 1)[0] for u in entry.unlocks}
        return (*(k not in kinds for k in ("patron", "variant", "champion")), order[entry.adventure.id],
                entry.patron is not None, entry.patron.id if entry.patron else 0)

    return tuple(sorted(found, key=rank))


def _unlocks(catalogue: Catalogue, account: Account, adventure: Adventure) -> tuple[str, ...]:
    """What completing `adventure`'s objective unlocks that I don't have yet."""
    def needs_it(requirements) -> bool:
        return any(
            isinstance(r, dict) and r.get("condition") == "adventure_complete"
            and str(r.get("adventure_id")) == str(adventure.id)
            for r in requirements
        )

    patrons = [
        f"patron {p.name}" for p in catalogue.patrons.values()
        if p.id not in account.unlocked_patron_ids and needs_it(p.requirements)
    ]
    variants = [
        f"variant {v.name}" for v in catalogue.adventures
        if v.is_variant and v.id != adventure.id and needs_it(v.requirements) and not is_done(account, v)
        and not account.best_areas.get(v.id)
    ]
    champion = []
    if adventure.champion_id and not adventure.is_variant and adventure.champion_id not in account.champions:
        champion = [f"champion {catalogue.champion(adventure.champion_id).name}"]
    return (*patrons, *variants, *champion)


# --- Steps -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FocusGain:
    adventure: Adventure
    gain_pct: float
    metric: str  # e.g. "Torm's Favor per hour"
    running: bool  # False: an event that isn't running


@dataclass(frozen=True)
class NextStep:
    change: Change
    gain_pct: float  # average relative gain over the Focus Set
    gains: tuple[FocusGain, ...]  # the Focus adventures it helps, best first
    why: str  # which seat or which buff to the Main DPS
    assumption: str  # the Scoring Assumption the gain rests on
    cost: str  # what it needs, against what the player holds
    unverified: bool  # a champion involved has no verified Champion Model
    source: str | None = None  # unowned champion: where it comes from
    obtainable: bool = True  # unowned champion: obtainable right now

    @property
    def reason(self) -> str:
        """`<action> → <metric change> on <Focus adventures it helps> (<why>)`, with its Scoring Assumption."""
        parts, metric = [], None
        for g in self.gains:
            shown = f" {g.metric}" if g.metric != metric else ""
            metric = g.metric
            parts.append(f"+{_pct(g.gain_pct)}%{shown} on {g.adventure.name}{'' if g.running else ' (event not running)'}")
        return f"{self.change.action} → {', '.join(parts)} ({self.why}). Scoring Assumption: {self.assumption}"


def next_steps(
    catalogue: Catalogue, account: Account, focus: Sequence[Adventure], measured: Mapping[int, Sequence[Hint]]
) -> tuple[NextStep, ...]:
    """Every step with a gain on the Focus Set, best average gain first. `measured`: each Focus
    adventure's hints (`measure(..., limit=None)` on its recommendation)."""
    if not focus:
        return ()
    by_action: dict[str, list[tuple[Adventure, Hint]]] = {}
    for adventure in focus:
        for hint in measured.get(adventure.id, ()):
            by_action.setdefault(hint.change.action, []).append((adventure, hint))
    steps = []
    for found in by_action.values():
        average = sum(h.gain_pct for _, h in found) / len(focus)
        if average <= MIN_GAIN_PCT:
            continue
        found.sort(key=lambda p: -p[1].gain_pct)
        change, best = found[0][1].change, found[0][1].recommendation
        steps.append(NextStep(
            change=change,
            gain_pct=average,
            gains=tuple(
                FocusGain(a, h.gain_pct, _metric(h.recommendation), is_running(catalogue, account, a)) for a, h in found
            ),
            why=_why(catalogue, change, best),
            assumption=best.scoring_assumptions[0] if best.scoring_assumptions else "",
            cost=_cost(catalogue, account, change),
            unverified=change.champion_id is not None
            and any(change.champion_id in {c.id for c in h.recommendation.unverified_champions} for _, h in found),
            source=catalogue.champion(change.champion_id).source.label if change.kind == "champion" else None,
            obtainable=change.kind != "champion" or _obtainable(catalogue, account, change.champion_id),
        ))
    steps.sort(key=lambda s: -s.gain_pct)
    return tuple(steps)


def measure_adventure(
    catalogue: Catalogue, account: Account, rec: FormationRecommendation, *, models: ChampionModels | None = None
) -> tuple[Hint, ...]:
    """One Focus adventure's hints for Next Steps: every one-change but potions, all of them."""
    rec = with_search_problem(catalogue, account, rec, models=models)
    return measure(catalogue, account, rec, candidate_changes(catalogue, account, potions=False), models=models, limit=None)


def _pct(value: float) -> str:
    return f"{value:.0f}" if value >= 10 else f"{value:.1f}" if value >= 0.1 else f"{value:.2f}"


def _metric(rec: FormationRecommendation) -> str:
    return rec.score_label.removesuffix(" (relative)")


def _why(catalogue: Catalogue, change: Change, rec: FormationRecommendation) -> str:
    main = rec.main_dps.champion.name if rec.main_dps else "the Main DPS"
    if change.kind == "modron":
        return f"modron core {change.core_id}'s nodes on the formation"
    seat = next((s for s in rec.seats if s.champion and s.champion.id == change.champion_id), None)
    if seat is None:
        return f"a better formation around {main}"
    if seat.is_main_dps:
        return f"{seat.champion.name} as the Main DPS, slot {seat.slot}"
    return f"{seat.champion.name} in slot {seat.slot}: {seat.contribution_label}" if seat.contribution_label else (
        f"{seat.champion.name} in slot {seat.slot}, buffing {main}"
    )


def _cost(catalogue: Catalogue, account: Account, change: Change) -> str:
    if change.kind == "gear" and "contract" in change.action.lower():
        held = account.buff_inventory.get(LARGE_CONTRACT_BUFF_ID, 0)
        return f"needs 1 Large Blacksmithing Contract ({CONTRACT_LEVELS} item levels), you have {held}"
    if change.kind == "gear":
        rarity = RARITY_NAMES.get(change.gear.rarity, "better")
        return f"needs a {rarity} item from a chest (random champion), you have {account.chests.get(GOLD_CHEST_ID, 0)} Gold Chests"
    if change.kind == "feat":
        return "needs the feat from the feat shop or its event (price not in the Game Definitions)"
    if change.kind == "champion":
        champion = catalogue.champion(change.champion_id)
        if _obtainable(catalogue, account, champion.id):
            return f"needs {champion.name} from {champion.source.label}"
        return f"needs {champion.name} from {champion.source.label}: not obtainable right now (event not running)"
    if change.kind == "modron":
        return f"needs {MODRON_LEVEL_EXP} core experience"
    return ""


def _obtainable(catalogue: Catalogue, account: Account, champion_id: int) -> bool:
    """Core and chest champions always; an event champion only while one of its adventures runs."""
    champion = catalogue.champion(champion_id)
    if champion.source.kind != "event":
        return True
    theirs = [a for a in catalogue.adventures if a.champion_id == champion_id]
    if theirs:
        return any(is_running(catalogue, account, a) for a in theirs)
    running = {c.name for c in catalogue.campaigns if c.is_event and c.event_id in account.running_event_ids}
    return champion.source.event_name in running


# --- background measuring ----------------------------------------------------------------------

StepKey = tuple[int, str]  # (adventure id, Run Goal)
ExecutorFactory = Callable[[Callable, tuple], Executor]


class Steps:
    """Each Focus adventure's measured hints per Run Goal, for the current data. With an
    `executor` (live mode) missing ones are measured in a background pool; otherwise on demand.
    Keyed like the Recompute: a Refresh that keeps the recommendation key keeps them."""

    def __init__(self, executor: ExecutorFactory | None = None) -> None:
        self._executor_factory = executor
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._run = 0
        self._raw: RawResponses | None = None
        self._worker_args: tuple = (None, {})  # the Area Set Cache and Model Statuses, for workers
        self._measured: dict[StepKey, tuple[Hint, ...]] = {}
        self._pending: set[StepKey] = set()
        self._pool: Executor | None = None

    def update(
        self, raw: RawResponses, account: Account, catalogue: Catalogue | None = None,
        models: ChampionModels | None = None,
    ) -> None:
        """New data loaded: drop everything measured on other data (and cancel its jobs). Workers
        get `catalogue`'s Area Set Cache (Push verdicts and breaking) and `models`' Model Statuses,
        and both are part of the key, as in the Recompute."""
        key = (account.recommendation_key, str(raw["getdefinitions"].get("checksum")))
        area_sets = dict(catalogue.area_sets) if catalogue is not None else None
        statuses = {k: v.value for k, v in models.statuses.items()} if models is not None else {}
        key += (_area_sets_key(catalogue) if catalogue is not None else "", _statuses_key(models) if models else "")
        with self._lock:
            self._raw = raw
            self._worker_args = (area_sets, statuses)
            if key == self._key:
                return
            self._key = key
            self._run += 1
            self._measured, self._pending = {}, set()
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def drop(self, adventure_id: int, goal: str) -> None:
        """The shown recommendation changed (e.g. "Prove it's the best"): measure again."""
        with self._lock:
            self._measured.pop((adventure_id, goal), None)

    def get(self, adventure_id: int, goal: str) -> tuple[Hint, ...] | None:
        with self._lock:
            return self._measured.get((adventure_id, goal))

    def pending(self, adventure_id: int, goal: str) -> bool:
        with self._lock:
            return (adventure_id, goal) in self._pending

    def put(self, adventure_id: int, goal: str, hints: tuple[Hint, ...]) -> None:
        with self._lock:
            self._measured[(adventure_id, goal)] = hints

    def ensure(self, wanted: Iterable[StepKey]) -> None:
        """Queue the background measuring of whatever is wanted and not measured or queued yet."""
        if self._executor_factory is None:
            return
        with self._lock:
            if self._raw is None:
                return
            missing = [k for k in dict.fromkeys(wanted) if k not in self._measured and k not in self._pending]
            if not missing:
                return
            self._pending.update(missing)
            run, raw = self._run, self._raw
            if self._pool is None:
                self._pool = self._executor_factory(_init_worker, (dict(raw), *self._worker_args))
            pool = self._pool
        for adventure_id, goal in missing:
            pool.submit(_measure_job, adventure_id, goal).add_done_callback(
                lambda future, k=(adventure_id, goal), run=run: self._collect(run, k, future)
            )

    def close(self) -> None:
        with self._lock:
            self._run += 1
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _collect(self, run: int, key: StepKey, future: Future) -> None:
        if future.cancelled():
            return
        try:
            hints = future.result()
        except Exception:
            hints = None  # left to measure on demand, where any error surfaces
        with self._lock:
            if run != self._run:
                return
            self._pending.discard(key)
            if hints is not None:
                self._measured.setdefault(key, hints)
            pool = self._pool if not self._pending else None
            if pool is not None:
                self._pool = None
        if pool is not None:
            pool.shutdown(wait=False)


# Worker side: each pool process parses the snapshot once, then measures one adventure per job.
_WORKER: tuple[Catalogue, Account, ChampionModels] | None = None


def _init_worker(raw: RawResponses, area_sets: dict | None = None, statuses: dict | None = None) -> None:
    """Like the Recompute's workers: the app's Area Set Cache and Model Statuses over the snapshot."""
    global _WORKER
    catalogue, account = parse_catalogue(raw), parse_account(raw)
    if area_sets is not None:
        catalogue = replace(catalogue, area_sets=area_sets)
    models = ChampionModels(catalogue, account, statuses={k: ModelStatus(v) for k, v in (statuses or {}).items()})
    _WORKER = (catalogue, account, models)


def _measure_job(adventure_id: int, goal: str) -> tuple[Hint, ...]:
    catalogue, account, models = _WORKER
    adventure = catalogue.adventure(adventure_id)
    if adventure is None:
        return ()
    return measure_adventure(catalogue, account, recommend(catalogue, account, adventure, goal, models=models), models=models)
