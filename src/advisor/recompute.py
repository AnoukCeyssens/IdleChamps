"""Recompute: Formation Recommendations precomputed in a background process pool (spec #22, #13).

After each Refresh every adventure × Run Goal (patron variants and events that aren't running
included) is computed, so opening an adventure is instant. Results are keyed on the account's recommendation key, the
definitions checksum and the advisor's own code, and persisted in the Store, so a restart (or a
currency-only Refresh) reuses them.

While a recompute runs, the results from before it stay visible, marked stale. A Refresh that
changes the key cancels the pending jobs and restarts from the new snapshot; the old pool is shut
down (its running jobs finished) before the new one starts, so two recomputes never run side by
side; Next Steps' background measuring shares the same pool (`background.Workers`). Anything not
precomputed (a job that failed) is computed on demand.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from concurrent.futures import Future
from dataclasses import dataclass

from advisor.account import Account
from advisor.background import DataKey, WorkerData, Workers, worker_data
from advisor.catalogue import Adventure, Catalogue
from advisor.champion_models import ChampionModels
from advisor.recommendation import FormationRecommendation, recommend
from advisor.request_log import RawResponses
from advisor.results_codec import Decoder, Encoder
from advisor.store import Store

_log = logging.getLogger(__name__)

ResultKey = tuple[int, str, int | None]  # (adventure id, Run Goal, patron id or None)


@dataclass(frozen=True)
class Progress:
    running: bool
    done: int  # adventures finished in the current recompute
    total: int
    completed: int  # bumps whenever a recompute finishes (the header reloads the page)


class Recompute:
    def __init__(self, goals: Iterable[str], *, store: Store | None = None, workers: Workers | None = None):
        """`goals`: the Run Goals to precompute. Without background `workers` nothing is
        precomputed (offline mode): results are computed on demand and kept until the data changes."""
        self._goals = tuple(goals)
        self._store = store
        self._workers = workers or Workers()
        self._lock = threading.Lock()  # guards the state below; held briefly
        self._update_lock = threading.Lock()  # one update at a time (pool shut down outside _lock)
        self._key: DataKey | None = None
        self._inputs: tuple[Catalogue, Account, ChampionModels] | None = None
        self._fresh: dict[ResultKey, FormationRecommendation] = {}
        self._old: dict[ResultKey, FormationRecommendation] = {}  # stale, shown while recomputing
        self._run = 0  # bumps on every key change: jobs of an earlier run are ignored
        self._running = False
        self._done = self._total = self._completed = 0
        self._complete = False  # every adventure × goal is in _fresh for the current key

    def update(
        self, raw: RawResponses, catalogue: Catalogue, account: Account, models: ChampionModels, *, precompute: bool
    ) -> None:
        """New data loaded. `precompute`: start a recompute if the results for it are missing
        (after a Refresh); otherwise only reuse what the Store holds (at start)."""
        key = DataKey.of(raw, catalogue, account, models)
        with self._update_lock:
            with self._lock:
                self._inputs = (catalogue, account, models)
                if key == self._key:  # e.g. a currency-only Refresh: keep results and any running recompute
                    if self._complete or self._running or not precompute:
                        return
                else:
                    self._key = key
                    self._run += 1
                    self._old = {**self._old, **self._fresh}
                    self._fresh, self._complete, self._running = {}, False, False
                run = self._run
            # Cancels the earlier data's pending jobs and waits for its running ones.
            self._workers.use(key, WorkerData.of(raw, catalogue, models))
            if self._load_persisted(key, catalogue):
                return
            if not precompute or not self._workers.enabled:
                with self._lock:
                    self._old, self._done, self._total = {}, 0, 0
                return
            self._start(run, catalogue)

    def get(self, adventure: Adventure, goal: str, patron: int | None = None) -> tuple[FormationRecommendation, bool]:
        """The recommendation and whether it is stale (from before the running recompute)."""
        key = (adventure.id, goal, patron)
        with self._lock:
            if key in self._fresh:
                return self._fresh[key], False
            if key in self._old:
                return self._old[key], True
            inputs, run = self._inputs, self._run
        catalogue, account, models = inputs
        result = recommend(catalogue, account, adventure, goal, patron, models=models)
        with self._lock:
            if run == self._run:
                self._fresh.setdefault(key, result)
        return result, False

    def put(self, adventure: Adventure, goal: str, patron: int | None, result: FormationRecommendation) -> None:
        """Replace one result for the current data (e.g. the exact solve's); a recompute
        still running keeps it rather than overwrite it."""
        with self._lock:
            self._fresh[(adventure.id, goal, patron)] = result
            self._old.pop((adventure.id, goal, patron), None)

    def available(self) -> list[tuple[FormationRecommendation, bool]]:
        """Every recommendation held now, without computing any: (recommendation, stale). A fresh
        result hides the stale one for the same adventure × goal × patron; once a recompute
        finished, every one is fresh."""
        with self._lock:
            old = [(rec, True) for key, rec in self._old.items() if key not in self._fresh]
            return [(rec, False) for rec in self._fresh.values()] + old

    def progress(self) -> Progress:
        with self._lock:
            return Progress(self._running, self._done, self._total, self._completed)

    def close(self) -> None:
        """Cancel any pending jobs (on app shutdown)."""
        with self._lock:
            self._run += 1
            self._running = False
        self._workers.close()

    def _start(self, run: int, catalogue: Catalogue) -> None:
        adventures = [a.id for a in catalogue.adventures]
        if not adventures:
            with self._lock:
                self._old, self._complete = {}, True
            return
        with self._lock:
            self._running, self._done, self._total = True, 0, len(adventures)
        for adventure_id in adventures:
            self._workers.submit(lambda future, run=run: self._collect(run, future), compute_job, adventure_id, self._goals)

    def _collect(self, run: int, future: Future) -> None:
        if future.cancelled():
            return
        try:
            results = future.result()
        except Exception:
            results = []  # left to compute on demand, where any error surfaces
        with self._lock:
            if run != self._run:
                return
            for key, result in results:
                self._fresh.setdefault(key, result)  # keep what was computed on demand or put meanwhile
            self._done += 1
            if self._done < self._total:
                return
            self._running = False
            self._old, self._complete = {}, True
            self._completed += 1
            key, fresh = self._key, dict(self._fresh)
        self._persist(key, fresh)

    def _persist(self, key: DataKey, results: dict[ResultKey, FormationRecommendation]) -> None:
        if self._store is None or self._inputs is None:
            return
        encode = Encoder(self._inputs[0])
        entries = []
        for (adventure_id, goal, patron), result in results.items():
            try:
                entries.append([adventure_id, goal, patron, encode(result)])
            except TypeError:
                continue  # a field the codec can't write yet: recomputed on demand after a restart
        try:
            self._store.save_results(key.to_json(), entries)
        except OSError as error:  # kept in memory; the next Refresh tries again
            _log.warning("Formation Recommendations not saved: %s", error)

    def _load_persisted(self, key: DataKey, catalogue: Catalogue) -> bool:
        if self._store is None:
            return False
        saved = self._store.load_results()
        if saved is None or list(saved[0]) != key.to_json():
            return False
        decode = Decoder(catalogue)
        try:
            results = {(a, g, p): decode(r) for a, g, p, r in saved[1]}
        except (KeyError, TypeError, ValueError):
            return False
        with self._lock:
            self._fresh.update(results)
            self._old, self._complete, self._done, self._total = {}, True, 0, 0
        return True


def compute_job(adventure_id: int, goals: tuple[str, ...]) -> list[tuple[ResultKey, FormationRecommendation]]:
    """Worker side: every goal's recommendation for one adventure and each of its patron variants."""
    catalogue, account, models = worker_data()
    adventure = catalogue.adventure(adventure_id)
    return [
        ((adventure_id, goal, patron), recommend(catalogue, account, adventure, goal, patron, models=models))
        for patron in (None, *adventure.patron_objectives)
        for goal in goals
    ]
