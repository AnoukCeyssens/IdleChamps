"""The shared background pool (`advisor.background.Workers`) across a data change.

A Refresh hands new data to the pool; that must not wait for jobs still running on the old data
(real accounts' jobs take minutes), yet two pools must never run side by side.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future

from advisor.background import Workers


class SlowPool:
    """A pool whose `shutdown(wait=True)` blocks until the test releases its running job."""

    def __init__(self, release: threading.Event, opened: list) -> None:
        self.release, self.submitted, self.closed = release, [], False
        opened.append(self)

    def submit(self, fn, *args) -> Future:
        future = Future()
        self.submitted.append((fn, args, future))
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        if wait:
            self.release.wait(5)
        self.closed = True


def _key(n):
    return ("key", n)


def test_new_data_does_not_wait_for_old_jobs_and_pools_never_overlap():
    release, opened = threading.Event(), []
    workers = Workers(lambda init, args: SlowPool(release, opened))
    done: list[Future] = []
    workers.use(_key(1), data=object())
    workers.submit(done.append, len, "old job")
    [old] = opened

    switched = threading.Event()
    threading.Thread(target=lambda: (workers.use(_key(2), data=object()), switched.set()), daemon=True).start()
    assert switched.wait(1), "a data change waited for the old pool's running job"

    workers.submit(done.append, len, "new job")
    assert len(opened) == 1  # the new job waits: no second pool while the old one drains

    release.set()  # the old job finishes; the old pool shuts down
    for _ in range(100):
        if len(opened) == 2:
            break
        threading.Event().wait(0.02)
    assert old.closed and len(opened) == 2
    assert [args for _, args, _ in opened[1].submitted] == [("new job",)]


def test_jobs_queued_for_superseded_data_are_cancelled():
    release, opened = threading.Event(), []
    workers = Workers(lambda init, args: SlowPool(release, opened))
    done: list[Future] = []
    workers.use(_key(1), data=object())
    workers.submit(done.append, len, "old job")
    workers.use(_key(2), data=object())  # old pool draining
    workers.submit(done.append, len, "job on key 2")
    workers.use(_key(3), data=object())  # key 2 superseded before it ever started

    assert [f.cancelled() for f in done] == [True]
    release.set()


class CountingPool:
    """Records submitted jobs; the test completes them."""

    def __init__(self, opened: list) -> None:
        self.submitted: list[tuple[str, Future]] = []
        opened.append(self)

    def submit(self, fn, *args) -> Future:
        future = Future()
        self.submitted.append((args[0], future))
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


def _finish(pool: CountingPool, name: str) -> None:
    future = next(f for n, f in pool.submitted if n == name)
    future.set_running_or_notify_cancel()
    future.set_result(None)


def test_with_limited_workers_urgent_jobs_run_before_earlier_queued_background_ones():
    """Next Steps' slow measuring jobs (queued first, at app start) must not hold every worker while
    the Recompute's adventures wait."""
    opened: list[CountingPool] = []
    workers = Workers(lambda init, args: CountingPool(opened), slots=2)
    workers.use(_key(1), data=object())
    for n in range(3):
        workers.submit(lambda f: None, str, f"steps {n}", priority=1)
    for n in range(3):
        workers.submit(lambda f: None, str, f"adventure {n}")  # priority 0: first

    [pool] = opened
    assert [n for n, _ in pool.submitted] == ["steps 0", "steps 1"]  # only 2 at a time
    _finish(pool, "steps 0")
    assert [n for n, _ in pool.submitted][2:] == ["adventure 0"]
    _finish(pool, "steps 1")
    _finish(pool, "adventure 0")
    assert [n for n, _ in pool.submitted][3:] == ["adventure 1", "adventure 2"]
