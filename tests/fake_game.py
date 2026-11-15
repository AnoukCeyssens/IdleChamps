"""A fake game server that replays recorded responses: fixture input for live Refresh tests.

It is a Game Client transport (`get(url, timeout) -> dict`), so the real live client runs
against it unchanged. Defaults come from the recorded fixture log; the calls the fixture
doesn't hold (`getcampaigndetails`, `getpatrondetails`, `geteventsdetails`,
`getcompletiondata`) get small credential-free bodies in the shapes documented in the
game-API research. Credentials are planted (fake values) where the real game puts them, so
tests can prove none of them is ever persisted.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from advisor.game_client import TransportError
from advisor.request_log import SEPARATOR, read_responses
from advisor.web import create_live_app

FIXTURE_LOG = Path(__file__).parent / "fixtures" / "requestlog.log.gz"

# Fake credentials, planted where the game puts real ones.
USER_ID = "7654321"
HASH = "deadbeefcafef00d1234"
INSTANCE_ID = "99887766"
PLAY_SERVER = "https://pslt1.idlechampions.com/~idledragons/"
SECRETS = (USER_ID, HASH, INSTANCE_ID)

_RECORDED = read_responses(FIXTURE_LOG)


def write_request_log(path: Path, *, user_id: str = USER_ID, hash: str = HASH) -> Path:
    """A request log in the game's format whose URL lines carry (fake) credentials."""
    blocks = [
        (
            "https://master.idlechampions.com/~idledragons/post.php?call=getPlayServerForDefinitions&network_id=11",
            '{"success": true}',
        ),
        (
            f"{PLAY_SERVER}post.php?call=getuserdetails&language_id=1&user_id={user_id}&hash={hash}&instance_key=1",
            '{"success": true}',
        ),
    ]
    path.write_text(SEPARATOR.join([""] + [f"{url}\n{body}\n" for url, body in blocks]), encoding="utf-8")
    return path


def user_details() -> dict:
    """The recorded getuserdetails, with credentials planted where the live server sends them."""
    body = copy.deepcopy(_RECORDED["getuserdetails"])
    body["details"]["instance_id"] = INSTANCE_ID
    for hero in body["details"]["heroes"]:
        hero["user_id"] = USER_ID
    body["linked_accounts"] = [{"network": "steam", "id": USER_ID}]
    return body


def definitions() -> dict:
    return copy.deepcopy(_RECORDED["getdefinitions"])


def _definitions_for(params: dict[str, str]) -> dict:
    """Like the server: nothing but the checksum when the client's checksum is current."""
    current = _RECORDED["getdefinitions"]["checksum"]
    if params.get("checksum") == str(current):
        return {"success": True, "checksum": current}
    return definitions()


DEFAULTS = {
    "getplayserverfordefinitions": lambda: copy.deepcopy(_RECORDED["getplayserverfordefinitions"]),
    "getuserdetails": user_details,
    "getcampaigndetails": lambda: {"success": True, "campaigns": [{"campaign_id": 1, "user_id": USER_ID}]},
    "getpatrondetails": lambda: {"success": True, "patrons": [{"patron_id": 1, "unlocked": True}]},
    "geteventsdetails": lambda: {"success": True, "events_details": {"active_events": []}},
    "getcompletiondata": lambda: {"success": True, "data": {"event": {}}},
}


@dataclass(frozen=True)
class Request:
    url: str
    call: str
    params: dict[str, str]
    at: float


class FakeClock:
    """Monotonic and wall time in one; `sleep` just moves time forward."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGameServer:
    """Replays responses per call; one-shot responses queued with `queue` are served first."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.requests: list[Request] = []
        self._queued: dict[str, list] = {}
        self.on_get = None  # optional hook called before each response (e.g. to block)

    def queue(self, call: str, *responses) -> None:
        """Serve these (dicts, or exceptions to raise) for the next requests of `call`."""
        self._queued.setdefault(call.lower(), []).extend(responses)

    def get(self, url: str, timeout: float) -> dict:
        parts = urlsplit(url)
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        call = params.get("call", "").lower()
        self.requests.append(Request(url, call, params, self.clock()))
        if self.on_get:
            self.on_get(call)
        queued = self._queued.get(call)
        if queued:
            response = queued.pop(0)
        elif call == "getdefinitions":
            response = _definitions_for(params)
        else:
            response = DEFAULTS[call]()
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def calls(self) -> list[str]:
        return [r.call for r in self.requests]


def unreachable() -> TransportError:
    return TransportError("connection refused")


class Live:
    """The app in live mode on a temporary data folder, talking to a `FakeGameServer`."""

    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock()
        self.server = FakeGameServer(self.clock)
        self.data_dir = tmp_path / "data"
        self.log = write_request_log(tmp_path / "webRequestLog.txt")
        self.data_dir.mkdir()
        (self.data_dir / "config.toml").write_text(f'request_log = "{self.log}"\n')

    def app(self, background=lambda job: job()):
        return create_live_app(
            self.data_dir, transport=self.server, clock=self.clock, sleep=self.clock.sleep, background=background
        )

    def start(self, **kwargs) -> TestClient:
        """The app as `advisor` starts it: entering the client runs the startup Refresh
        (synchronously here, so tests see its result)."""
        client = TestClient(self.app(**kwargs))
        client.__enter__()
        return client
