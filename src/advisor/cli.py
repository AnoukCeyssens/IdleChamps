"""`advisor`: start the advisor on localhost.

    uv run advisor --from-log              # offline mode on the game's own request log
    uv run advisor --from-log PATH         # offline mode on another log or a fixture (.gz ok)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from advisor.game_client import DEFAULT_LOG
from advisor.web import create_app

HOST = "127.0.0.1"  # localhost only, never configurable


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="advisor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--from-log",
        nargs="?",
        const=DEFAULT_LOG,
        type=Path,
        metavar="PATH",
        help="offline mode: read the game's request log instead of calling the servers",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.from_log is None:
        parser.error("live mode is not built yet; run with --from-log for offline mode")

    app = create_app(args.from_log)
    print(f"Idle Champions Advisor (offline): http://{HOST}:{args.port}/")
    uvicorn.run(app, host=HOST, port=args.port)
