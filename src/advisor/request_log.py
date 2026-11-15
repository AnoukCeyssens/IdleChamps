"""Reading and writing the game's request log (`webRequestLog.txt`).

The log is a series of blocks separated by a line of 50 asterisks. Each block is a request
URL line followed by the JSON response body. Request URLs carry credentials, so only the
call name is ever read from them; everything else comes from response bodies.

A credential-free fixture log uses the same format (optionally gzipped), with each URL line
reduced to `call=<name>`.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SEPARATOR = "*" * 50

# Response fields that identify or authenticate the player. They are dropped wherever they
# occur, at any depth, before anything leaves memory.
CREDENTIAL_FIELDS = frozenset(
    {
        "user_id",
        "hash",
        "instance_id",
        "instance_key",
        "internal_user_id",
        "unlinked_user_id",
        "steam_user_id",
        "steam_name",
        "auth_ticket",
        "device_hash",
        "chat_socket_hash",
        "linked_accounts",
        "newsletter_email",
        "newsletter_link_code",
        "player_name",
        "twitch_link_url",
        "rlca",
        "local_device_install_time",
    }
)

RawResponses = Mapping[str, Any]
"""Latest response body per call, keyed by lower-case call name (e.g. `getuserdetails`)."""

_CALL = re.compile(r"call=(\w+)")


def read_responses(path: Path) -> dict[str, Any]:
    """Parse a request log (plain or gzipped) into the latest response body per call."""
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    responses: dict[str, Any] = {}
    for block in raw.decode("utf-8").split(SEPARATOR):
        lines = [line for line in block.strip().split("\n") if line.strip()]
        if len(lines) < 2:
            continue
        match = _CALL.search(lines[0])
        if not match:
            continue
        try:
            body = json.loads(lines[1])
        except json.JSONDecodeError:
            continue
        responses[match.group(1).lower()] = body
    return responses


def strip_credentials(value: Any) -> Any:
    """Return a copy of a response body with every credential field removed, at any depth."""
    if isinstance(value, dict):
        return {k: strip_credentials(v) for k, v in value.items() if k not in CREDENTIAL_FIELDS}
    if isinstance(value, list):
        return [strip_credentials(v) for v in value]
    return value


def write_responses(responses: RawResponses, path: Path) -> None:
    """Write response bodies as a request log, gzipped when `path` ends in `.gz`."""
    blocks = [""] + [f"call={call}\n{json.dumps(body)}\n" for call, body in responses.items()]
    data = SEPARATOR.join(blocks).encode("utf-8")
    if path.suffix == ".gz":
        data = gzip.compress(data, mtime=0)
    path.write_bytes(data)
