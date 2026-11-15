"""Background work in live mode: one process pool, shared by the Recompute and Next Steps.

Both precompute on the same data (the snapshot, the Area Set Cache the app holds and the Model
Statuses validation gave), keyed the same way (`DataKey`). Their jobs share one pool (`Workers`),
so two recomputes never run side by side: a data change cancels the pending jobs, and jobs on the
new data wait (queued) until the old pool's running jobs have finished. The data change itself
never waits: a Refresh returns at once even when a job takes minutes.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import os
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from dataclasses import astuple, dataclass, replace
from pathlib import Path

from advisor import area_sets
from advisor.account import Account, parse_account
from advisor.catalogue import Catalogue, parse_catalogue
from advisor.champion_models import ChampionModels, ModelStatus
from advisor.request_log import RawResponses

ExecutorFactory = Callable[[Callable, tuple], Executor]  # (worker initializer, its args) -> pool


WORKER_COUNT = max(1, (os.cpu_count() or 2) - 1)


def process_pool(initializer: Callable, initargs: tuple) -> Executor:
    return ProcessPoolExecutor(max_workers=WORKER_COUNT, initializer=initializer, initargs=initargs)


def _code_fingerprint() -> str:
    """Hash of the advisor's source: results computed by other code are never reused."""
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted([*root.rglob("*.py"), *(p for p in (root / "curated").rglob("*") if p.is_file())]):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


CODE = _code_fingerprint()


def _fingerprint(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class WorkerData:
    """What a worker process rebuilds the app's data from: the snapshot, the Area Set Cache the app
    holds (live mode keeps more sets than one snapshot brings) and the Model Statuses validation
    gave (champion id -> status value)."""

    raw: RawResponses
    area_sets: Mapping
    statuses: Mapping[int, str]

    @classmethod
    def of(cls, raw: RawResponses, catalogue: Catalogue, models: ChampionModels) -> WorkerData:
        return cls(dict(raw), dict(catalogue.area_sets), {k: v.value for k, v in models.statuses.items()})

    def load(self) -> tuple[Catalogue, Account, ChampionModels]:
        catalogue = replace(parse_catalogue(self.raw), area_sets=dict(self.area_sets))
        account = parse_account(self.raw)
        statuses = {k: ModelStatus(v) for k, v in self.statuses.items()}
        return catalogue, account, ChampionModels(catalogue, account, statuses=statuses)


@dataclass(frozen=True)
class DataKey:
    """What precomputed results depend on. A Refresh that keeps it (e.g. currency-only) keeps them."""

    recommendation_key: str
    definitions_checksum: str
    code: str
    area_sets: str  # the Area Set Cache: a set learnt or overwritten changes Push verdicts
    statuses: str  # Model Statuses change badges and Push bands (#38)

    @classmethod
    def of(cls, raw: RawResponses, catalogue: Catalogue, account: Account, models: ChampionModels) -> DataKey:
        return cls(
            account.recommendation_key,
            str(raw["getdefinitions"].get("checksum")),
            CODE,
            _fingerprint(area_sets.to_json(catalogue.area_sets)),
            _fingerprint({str(k): v.value for k, v in models.statuses.items()}),
        )

    def to_json(self) -> list[str]:
        return list(astuple(self))


class Workers:
    """The one background pool (live mode; `executor=None` means no background work). It starts on
    the first job and shuts down once idle. Jobs wait here and are fed to the pool at most `slots`
    at a time (None: all at once), lowest `priority` first, so slow low-priority jobs queued early
    can't hold every worker. `use` with new data cancels waiting jobs and returns at once; the old
    pool drains in the background (`drain`), and jobs on the new data wait until it has, so pools
    never overlap."""

    def __init__(
        self,
        executor: ExecutorFactory | None = None,
        *,
        drain: Callable[[Callable[[], None]], None] | None = None,
        slots: int | None = None,
    ) -> None:
        self._factory = executor
        self._drain_in = drain or _in_thread
        self._slots = slots
        self._draining = False
        self._waiting: list[tuple[int, int, Callable[[Future], None], Callable, tuple]] = []  # a heap
        self._order = itertools.count()
        self._lock = threading.Lock()
        self._use_lock = threading.Lock()
        self._key: DataKey | None = None
        self._data: WorkerData | None = None
        self._pool: Executor | None = None
        self._running = 0  # jobs handed to the current pool and not yet done

    @property
    def enabled(self) -> bool:
        return self._factory is not None

    def use(self, key: DataKey, data: WorkerData) -> None:
        with self._use_lock:
            with self._lock:
                if key == self._key:
                    return
                self._key, self._data = key, data
                pool, self._pool, self._running = self._pool, None, 0
                superseded, self._waiting = self._waiting, []
                if pool is not None:
                    self._draining = True
            for _, _, on_done, _, _ in superseded:  # waiting on data that is now out of date
                cancelled: Future = Future()
                cancelled.cancel()
                on_done(cancelled)
            if pool is not None:
                self._drain_in(lambda: self._drain(pool))

    def _drain(self, pool: Executor) -> None:
        """Wait out the old data's running jobs (pending ones are cancelled), then start the jobs
        waiting on the current data."""
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        finally:
            with self._lock:
                self._draining = False
            self._dispatch()

    def submit(self, on_done: Callable[[Future], None], fn: Callable, *args, priority: int = 0) -> None:
        """Run `fn(*args)` in a worker on the current data; `on_done(future)` when it ends (also
        when cancelled by a data change). Lower `priority` runs first."""
        with self._lock:
            if self._factory is None or self._data is None:
                return
            heapq.heappush(self._waiting, (priority, next(self._order), on_done, fn, args))
        self._dispatch()

    def _dispatch(self) -> None:
        """Hand waiting jobs to the pool while it has free slots."""
        started = []
        with self._lock:
            while (
                self._waiting
                and not self._draining
                and self._data is not None
                and (self._slots is None or self._running < self._slots)
            ):
                _, _, on_done, fn, args = heapq.heappop(self._waiting)
                if self._pool is None:
                    self._pool = self._factory(_init_worker, (self._data,))
                self._running += 1
                started.append((self._pool, self._pool.submit(fn, *args), on_done))
        for pool, future, on_done in started:
            future.add_done_callback(lambda f, pool=pool, on_done=on_done: self._done(pool, f, on_done))

    def close(self) -> None:
        with self._lock:
            pool, self._pool, self._key = self._pool, None, None
            self._waiting = []
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _done(self, pool: Executor, future: Future, on_done: Callable[[Future], None]) -> None:
        try:
            on_done(future)
        finally:
            idle = False
            with self._lock:
                if pool is self._pool:
                    self._running -= 1
                    if self._running <= 0 and not self._waiting:
                        self._pool, idle = None, True
            if idle:
                pool.shutdown(wait=False)
            else:
                self._dispatch()


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="drain-pool", daemon=True).start()


# Worker side: each pool process rebuilds the data once, then runs jobs on it.
_WORKER: tuple[Catalogue, Account, ChampionModels] | None = None


def _init_worker(data: WorkerData) -> None:
    global _WORKER
    _WORKER = data.load()


def worker_data() -> tuple[Catalogue, Account, ChampionModels]:
    """The data this worker process was started on (catalogue, account, models)."""
    assert _WORKER is not None, "not in a background worker"
    return _WORKER
