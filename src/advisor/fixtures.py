"""Build credential-free test fixtures from a recorded request log.

    uv run advisor-build-fixture [RAW_LOG] tests/fixtures/requestlog.log.gz

Keeps only allow-listed calls, keeps only response bodies, and strips credential fields.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from advisor.game_client import DEFAULT_LOG, OfflineGameClient
from advisor.request_log import strip_credentials, write_responses


def build_fixture(raw_log: Path, out: Path) -> None:
    responses = OfflineGameClient(raw_log).fetch()
    write_responses({call: strip_credentials(body) for call, body in responses.items()}, out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("raw_log", nargs="?", type=Path, default=DEFAULT_LOG)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    build_fixture(args.raw_log, args.out)
    print(f"wrote {args.out}")
