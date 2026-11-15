"""Recompute: Formation Recommendations precomputed in a background process pool (spec #22, #13).

After each Refresh every adventure × Run Goal (events that aren't running included) is computed,
so opening an adventure is instant. Results are keyed on the account's recommendation key, the
definitions checksum and the advisor's own code, and persisted in the Store, so a restart (or a
currency-only Refresh) reuses them.

While a recompute runs, the results from before it stay visible, marked stale. A Refresh that
changes the key cancels the pending jobs and restarts from the new snapshot; the old pool is shut
down (its running jobs finished) before the new one starts, so two recomputes never run side by
side. Anything not precomputed (a patron variant, a job that failed) is computed on demand.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from advisor.account import Account, parse_account
from advisor.catalogue import Adventure, Catalogue, parse_catalogue
from advisor.champion_models import ChampionModels
from advisor.recommendation import FormationRecommendation, recommend
from advisor.request_log import RawResponses
from advisor.results_codec import Decoder, Encoder
from advisor.store import Store

ResultKey = tuple[int, str, int | None]  # (adventure id, Run Goal, patron id or None)
ExecutorFactory = Callable[[Callable, tuple], Executor]  # (worker initializer, its args) -> pool


def process_pool(initializer: Callable, initargs: tuple) -> Executor:
    return ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1), initializer=initializer, initargs=initargs)


def _code_fingerprint() -> str:
    """Hash of the advisor's source: results computed by other code are never reused."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


CODE = _code_fingerprint()


@dataclass(frozen=True)
class Progress:
    running: bool
    done: int  # adventures finished in the current recompute
    total: int
    completed: int  # bumps whenever a recompute finishes (the header reloads the page)


class Recompute:
    def __init__(self, goals: Iterable[str], *, store: Store | None = None, executor: ExecutorFactory | None = None):
        """`goals`: the Run Goals to precompute. Without `executor` nothing is precomputed
        (offline mode): results are computed on demand and kept until the data changes."""
        self._goals = tuple(goals)
        self._store = store
        self._executor_factory = executor
        self._lock = threading.Lock()  # guards the state below; held briefly
        self._update_lock = threading.Lock()  # one update at a time (pool shut down outside _lock)
        self._key: tuple | None = None
        self._inputs: tuple[Catalogue, Account, ChampionModels] | None = None
        self._fresh: dict[ResultKey, FormationRecommendation] = {}
        self._old: dict[ResultKey, FormationRecommendation] = {}  # stale, shown while recomputing
        self._run = 0  # bumps on every key change: jobs of an earlier run are ignored
        self._pool: Executor | None = None
        self._done = self._total = self._completed = 0
        self._complete = False  # every adventure × goal is in _fresh for the current key

    def update(
        self, raw: RawResponses, catalogue: Catalogue, account: Account, models: ChampionModels, *, precompute: bool
    ) -> None:
        """New data loaded. `precompute`: start a recompute if the results for it are missing
        (after a Refresh); otherwise only reuse what the Store holds (at start)."""
        key = (account.recommendation_key, str(raw["getdefinitions"].get("checksum")), CODE)
        with self._update_lock:
            with self._lock:
                self._inputs = (catalogue, account, models)
                if key == self._key:  # e.g. a currency-only Refresh: keep results and any running recompute
                    if self._complete or self._pool is not None or not precompute:
                        return
                else:
                    self._key = key
                    self._run += 1
                    self._old = {**self._old, **self._fresh}
                    self._fresh, self._complete = {}, False
                old_pool, self._pool = self._pool, None
                run = self._run
            if old_pool is not None:  # outside _lock: its finishing jobs report through _collect
                old_pool.shutdown(wait=True, cancel_futures=True)
            if self._load_persisted(key, catalogue):
                return
            if not precompute or self._executor_factory is None:
                with self._lock:
                    self._old, self._done, self._total = {}, 0, 0
                return
            self._start(run, raw, catalogue)

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

    def progress(self) -> Progress:
        with self._lock:
            return Progress(self._pool is not None, self._done, self._total, self._completed)

    def close(self) -> None:
        """Cancel any pending jobs (on app shutdown)."""
        with self._lock:
            self._run += 1
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _start(self, run: int, raw: RawResponses, catalogue: Catalogue) -> None:
        adventures = [a.id for a in catalogue.adventures]
        if not adventures:
            with self._lock:
                self._old, self._complete = {}, True
            return
        pool = self._executor_factory(_init_worker, (dict(raw),))
        with self._lock:
            self._pool, self._done, self._total = pool, 0, len(adventures)
        for adventure_id in adventures:
            pool.submit(_compute, adventure_id, self._goals).add_done_callback(
                lambda future, run=run: self._collect(run, future)
            )

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
            pool, self._pool = self._pool, None
            self._old, self._complete = {}, True
            self._completed += 1
            key, fresh = self._key, dict(self._fresh)
        if pool is not None:
            pool.shutdown(wait=False)
        self._persist(key, fresh)

    def _persist(self, key: tuple, results: dict[ResultKey, FormationRecommendation]) -> None:
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
            self._store.save_results(list(key), entries)
        except OSError:
            pass  # kept in memory; the next Refresh tries again

    def _load_persisted(self, key: tuple, catalogue: Catalogue) -> bool:
        if self._store is None:
            return False
        saved = self._store.load_results()
        if saved is None or tuple(saved[0]) != key:
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


# Worker side: each pool process parses the snapshot once, then computes one adventure per job.
_WORKER: tuple[Catalogue, Account, ChampionModels] | None = None


def _init_worker(raw: RawResponses) -> None:
    global _WORKER
    catalogue, account = parse_catalogue(raw), parse_account(raw)
    _WORKER = (catalogue, account, ChampionModels(catalogue, account))


def _compute(adventure_id: int, goals: tuple[str, ...]) -> list[tuple[ResultKey, FormationRecommendation]]:
    catalogue, account, models = _WORKER
    adventure = catalogue.adventure(adventure_id)
    return [((adventure_id, goal, None), recommend(catalogue, account, adventure, goal, models=models)) for goal in goals]
