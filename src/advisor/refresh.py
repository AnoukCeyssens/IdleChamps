"""Refresh: fetch a new Player Snapshot (and changed Game Definitions) into the Store.

One Refresh runs at a time; a request while one runs, or during the cooldown after a
successful one, is ignored. A failed Refresh keeps the cached data and records why it
failed. It never falls back to the request log's own responses.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from advisor.game_client import LiveGameClient, RefreshError
from advisor.request_log import RawResponses
from advisor.store import Snapshot, Store, merge_definitions

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshStatus:
    running: bool
    refreshed_at: float | None  # last successful Refresh (this session or a cached one)
    error: RefreshError | None  # why the latest Refresh failed, until one succeeds
    cooldown_left: int  # whole seconds until the next Refresh is allowed
    generation: int  # bumps on every successful Refresh


def _in_thread(job: Callable[[], Any]) -> None:
    threading.Thread(target=job, name="refresh", daemon=True).start()


class Refresher:
    def __init__(
        self,
        client: LiveGameClient,
        store: Store,
        *,
        cooldown: float = 60,
        clock: Callable[[], float] = time.time,
        background: Callable[[Callable[[], Any]], None] = _in_thread,
    ) -> None:
        self._client = client
        self._store = store
        self._cooldown = cooldown
        self._clock = clock
        self._background = background
        self._lock = threading.Lock()
        self._listeners: list[Callable[[RawResponses], None]] = []
        self._succeeded_at: float | None = None  # this session only: drives the cooldown
        self._error: RefreshError | None = None
        self._generation = 0
        snapshot = store.load_snapshot()
        self._refreshed_at = snapshot.refreshed_at if snapshot else None

    def responses(self) -> RawResponses:
        """The cached responses (snapshot + definitions), or {} before the first Refresh."""
        snapshot, definitions = self._store.load_snapshot(), self._store.load_definitions()
        if snapshot is None or definitions is None:
            return {}
        return {**snapshot.responses, "getdefinitions": {**definitions.tables, "checksum": definitions.checksum}}

    def on_refresh(self, listener: Callable[[RawResponses], None]) -> None:
        """Call `listener` with the new responses after each successful Refresh."""
        self._listeners.append(listener)

    def start(self) -> None:
        """Refresh in the background (on app start)."""
        self._background(self.refresh)

    def refresh(self) -> bool:
        """Run one Refresh now. False when ignored (one is running, or in cooldown)."""
        if self.status().cooldown_left or not self._lock.acquire(blocking=False):
            return False
        try:
            self._run()
            return True
        finally:
            self._lock.release()

    def now(self) -> float:
        """Wall-clock time as the Refresher sees it (for "refreshed N s ago")."""
        return self._clock()

    def status(self) -> RefreshStatus:
        left = 0
        if self._succeeded_at is not None:
            left = max(0, int(self._succeeded_at + self._cooldown - self._clock() + 0.999))
        return RefreshStatus(self._lock.locked(), self._refreshed_at, self._error, left, self._generation)

    def _run(self) -> None:
        cached, last_snapshot = self._store.load_definitions(), self._store.load_snapshot()
        try:
            raw = self._client.refresh(cached.checksum if cached else None)
            now = self._clock()
            definitions = merge_definitions(cached, raw["getdefinitions"])
            self._store.save_definitions(definitions)
            self._store.save_snapshot(Snapshot(now, {k: v for k, v in raw.items() if k != "getdefinitions"}))
        except RefreshError as error:
            self._error = error
            return
        except OSError:
            self._error = RefreshError(
                "The advisor couldn't save to its data folder.", "Check the disk has free space, then Refresh."
            )
            return
        except Exception as error:  # an unexpected response shape; keep the cached data
            self._error = RefreshError(
                f"The game's answer couldn't be read ({type(error).__name__}).", "Refresh again later."
            )
            return
        responses = self.responses()
        try:
            for listener in self._listeners:
                listener(responses)
        except Exception as error:  # the answer arrived but couldn't be parsed; the last good data stays loaded
            log.exception("Refresh: the game's answer couldn't be read")
            if cached is not None and last_snapshot is not None:  # don't leave the unreadable answer on disk
                self._store.save_definitions(cached)
                self._store.save_snapshot(last_snapshot)
            self._error = RefreshError(
                f"The game's answer couldn't be read ({type(error).__name__}).", "Refresh again later."
            )
            return
        self._error = None
        self._succeeded_at = self._refreshed_at = now
        self._generation += 1
