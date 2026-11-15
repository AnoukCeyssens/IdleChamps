"""Game Client: the only source of raw game responses.

v1 so far has offline mode only: it reads the game's request log instead of calling the
servers. The live client (allow-listed GET calls, credentials in memory only) comes later.
"""

from __future__ import annotations

from pathlib import Path

from advisor.request_log import RawResponses, read_responses

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
