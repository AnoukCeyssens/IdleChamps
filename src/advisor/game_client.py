"""Game Client: the only source of raw game responses.

Two clients: `OfflineGameClient` reads the game's request log (offline mode), and
`LiveGameClient` makes the Refresh calls to the game servers. The live client only ever
sends GET requests for the calls in `READ_ONLY_CALLS`; no write call exists anywhere.
Credentials are read from the request log at Refresh time and held in memory only.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from advisor.request_log import RawResponses, read_credentials, read_responses

# The hard allow-list of read-only calls (lower-case). Nothing else is ever read or made.
READ_ONLY_CALLS = frozenset(
    {
        "getplayserverfordefinitions",
        "getuserdetails",
        "getcampaigndetails",
        "getpatrondetails",
        "geteventsdetails",
        "getdefinitions",
        "getcompletiondata",
    }
)

DEFAULT_LOG = Path.home() / (
    "Library/Application Support/Steam/steamapps/common/IdleChampions/IdleDragonsMac.app/"
    "Contents/Resources/Data/StreamingAssets/downloaded_files/webRequestLog.txt"
)


class OfflineGameClient:
    """Serves raw responses from a request log (the game's own, or a fixture). No network."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path

    def fetch(self) -> RawResponses:
        """The latest response per allow-listed call found in the log."""
        return {
            call: body for call, body in read_responses(self.log_path).items() if call in READ_ONLY_CALLS
        }


MASTER_SERVER = "https://master.idlechampions.com/~idledragons/"
CALL_SPACING = 0.2  # seconds between two calls
MAX_RETRIES = 4  # per call, after the first attempt
TIMEOUT = 20.0  # seconds per request

# Parameters every community client sends with each call.
_BOILERPLATE = {
    "language_id": "1",
    "timestamp": "0",
    "request_id": "0",
    "mobile_client_version": "99999",
    "include_free_play_objectives": "true",
    "instance_key": "1",
    "offline_v2_build": "1",
    "localization_aware": "true",
}

_LAUNCH_THE_GAME = "Launch Idle Champions once, then Refresh."


class TransportError(Exception):
    """The request didn't get a JSON answer (no network, dead server, HTTP error)."""


class CallNotAllowed(Exception):
    """A call outside the read-only allow-list was attempted. It was never sent."""


@dataclass(frozen=True)
class RefreshError(Exception):
    """Why a Refresh failed and what the player can do about it. Never carries credentials."""

    cause: str
    fix: str

    def __str__(self) -> str:
        return f"{self.cause} {self.fix}"


class Transport(Protocol):
    def get(self, url: str, timeout: float) -> dict[str, Any]: ...


class HttpTransport:
    """GET over HTTPS with the standard library. The only network code in the app."""

    def get(self, url: str, timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "idlechamps-advisor"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as error:  # URLError, HTTPError, timeouts, bad JSON
            # The message never includes the URL: it carries credentials.
            raise TransportError(type(error).__name__) from None


class LiveGameClient:
    """Read-only calls to the game servers, one at a time, ~200 ms apart.

    The play server is discovered once per session and followed when the game says
    `switch_play_server`. Each call is retried at most `MAX_RETRIES` times.
    """

    def __init__(
        self,
        transport: Transport,
        request_log: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._request_log = request_log
        self._clock = clock
        self._sleep = sleep
        self._play_server: str | None = None
        self._last_call_at: float | None = None

    def refresh(self, definitions_checksum: str | None) -> RawResponses:
        """One Refresh: the latest response per call. Definitions are a delta against
        `definitions_checksum` when one is given (the full set otherwise)."""
        session = _Session(self, self._credentials())
        responses: dict[str, Any] = {}
        responses["getuserdetails"] = session.user_details()
        for call in ("getcampaigndetails", "getpatrondetails", "geteventsdetails"):
            responses[call] = session.call(call, instance=True)
        responses["getcompletiondata"] = session.call("getcompletiondata", {"level": "0"}, instance=True)
        params = {"supports_chunked_defs": "0", "new_achievements": "1", "challenge_sets_no_deltas": "0"}
        if definitions_checksum is not None:
            params["checksum"] = str(definitions_checksum)
        responses["getdefinitions"] = session.call("getdefinitions", params, auth=False)
        if session.user_details_refetched is not None:
            responses["getuserdetails"] = session.user_details_refetched
        return responses

    def _credentials(self) -> dict[str, str]:
        credentials = read_credentials(self._request_log)
        if credentials is None:
            raise RefreshError("No Idle Champions login was found in the game's request log.", _LAUNCH_THE_GAME)
        return credentials

    def play_server(self) -> str:
        if self._play_server is None:
            server = self.get(MASTER_SERVER, "getPlayServerForDefinitions", {}).get("play_server")
            if not server:
                raise TransportError("no play server offered")
            self._play_server = _https(server)
        return self._play_server

    def forget_play_server(self) -> None:
        self._play_server = None

    def switch_play_server(self, server: str) -> None:
        self._play_server = _https(server)

    def get(self, server: str, call: str, params: dict[str, str]) -> dict[str, Any]:
        """Send one allow-listed GET call, spaced from the previous one. The only way out."""
        if call.lower() not in READ_ONLY_CALLS:
            raise CallNotAllowed(call)
        url = f"{server}post.php?" + urlencode({"call": call, **_BOILERPLATE, **params})
        self._space()
        return self._transport.get(url, TIMEOUT)

    def _space(self) -> None:
        if self._last_call_at is not None:
            wait = CALL_SPACING - (self._clock() - self._last_call_at)
            if wait > 0:
                self._sleep(wait)
        self._last_call_at = self._clock()


class _Session:
    """One Refresh's credentials and instance id, in memory only, dropped when it ends."""

    def __init__(self, client: LiveGameClient, credentials: dict[str, str]) -> None:
        self._client = client
        self._credentials = credentials
        self._instance_id: str | None = None
        self.user_details_refetched: dict[str, Any] | None = None

    def user_details(self) -> dict[str, Any]:
        body = self.call("getuserdetails")
        self._instance_id = str(body.get("details", {}).get("instance_id", "")) or None
        return body

    def call(
        self, call: str, params: dict[str, str] | None = None, *, auth: bool = True, instance: bool = False
    ) -> dict[str, Any]:
        rediscovered = False
        for _attempt in range(MAX_RETRIES + 1):
            query = dict(params or {})
            if auth:
                query.update(self._credentials)
            if instance and self._instance_id:
                query["instance_id"] = self._instance_id
            try:
                body = self._client.get(self._client.play_server(), call, query)
            except TransportError:
                if not rediscovered:  # the shard may be dead: discover the play server again, once
                    rediscovered = True
                    self._client.forget_play_server()
                continue
            if body.get("switch_play_server"):
                self._client.switch_play_server(body["switch_play_server"])
                continue
            if body.get("success") is False or body.get("failure_reason"):
                reason = str(body.get("failure_reason") or "")
                if "outdated instance" in reason.lower() and call.lower() != "getuserdetails":
                    self.user_details_refetched = self.user_details()
                    continue
                raise _failure(call, reason)
            return body
        raise RefreshError("The game servers can't be reached.", "Check your internet connection, then Refresh.")


def _failure(call: str, reason: str) -> RefreshError:
    lowered = reason.lower()
    if "hash" in lowered:  # "Security hash failure"
        return RefreshError("The game rejected the login saved in its request log (it is stale).", _LAUNCH_THE_GAME)
    if "non-atomic" in lowered:
        return RefreshError("The game servers were busy with your running game.", "Refresh again in a moment.")
    return RefreshError(f"The game servers refused {call}: {reason or 'no reason given'}.", "Refresh again later.")


def _https(server: str) -> str:
    server = server.replace("http://", "https://", 1)
    return server if server.endswith("/") else server + "/"
