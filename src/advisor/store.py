"""Store: the app data folder.

One folder holds everything the advisor keeps between runs, as gzipped JSON with a version
field, each file written atomically (temp file + rename):

- `definitions.json.gz`: the Game Definitions cache (tables + checksum), merged with every
  delta a Refresh brings.
- `snapshot.json.gz`: the latest credential-stripped Player Snapshot subset (the Refresh's
  responses minus definitions, shop/promo and server stats).
- `area_sets.json.gz`: the Area Set Cache (monster placement per `area_set_id`, each with the game
  version it was last seen under); grows with every Refresh, never expires (`advisor.area_sets`).
- `history.jsonl.gz`: append-only History Records, one JSON line each (one gzip member per
  record), each line with its own version field.
- `config.toml`: configurable values (written by hand; there is no settings page).

Credentials are stripped before anything is written.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from advisor import area_sets
from advisor.request_log import strip_credentials

DEFINITIONS_VERSION = 1
SNAPSHOT_VERSION = 1
HISTORY_FILE = "history.jsonl.gz"

DEFAULT_DATA_DIR = Path.home() / "Library/Application Support/IdleChampsAdvisor"

# Top-level response keys that are never kept: shop and promo data, and server bookkeeping.
_NOT_KEPT = frozenset(
    {"current_sales", "actions", "memory_usage", "apc_stats", "db_stats", "processing_time", "linked_accounts"}
)


@dataclass(frozen=True)
class Config:
    request_log: Path  # where the game writes webRequestLog.txt (credentials are read from it)
    cooldown_seconds: int = 60  # after a successful Refresh


def load_config(data_dir: Path, default_request_log: Path) -> Config:
    """`config.toml` in the app data folder; every key is optional."""
    path = data_dir / "config.toml"
    values = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return Config(
        request_log=Path(values.get("request_log", default_request_log)).expanduser(),
        cooldown_seconds=int(values.get("cooldown_seconds", 60)),
    )


@dataclass(frozen=True)
class Definitions:
    checksum: str | None
    tables: Mapping[str, Any]  # every `*_defines*` table by name


@dataclass(frozen=True)
class Snapshot:
    refreshed_at: float  # wall-clock seconds
    responses: Mapping[str, Any]  # credential-stripped response body per call


class Store:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def load_definitions(self) -> Definitions | None:
        data = self._read("definitions.json.gz", DEFINITIONS_VERSION)
        return Definitions(data["checksum"], data["tables"]) if data else None

    def save_definitions(self, definitions: Definitions) -> None:
        self._write(
            "definitions.json.gz",
            {"version": DEFINITIONS_VERSION, "checksum": definitions.checksum, "tables": definitions.tables},
        )

    def load_snapshot(self) -> Snapshot | None:
        data = self._read("snapshot.json.gz", SNAPSHOT_VERSION)
        return Snapshot(data["refreshed_at"], data["responses"]) if data else None

    def save_snapshot(self, snapshot: Snapshot) -> None:
        responses = {call: _kept(body) for call, body in snapshot.responses.items()}
        self._write(
            "snapshot.json.gz",
            {"version": SNAPSHOT_VERSION, "refreshed_at": snapshot.refreshed_at, "responses": responses},
        )

    def load_area_sets(self) -> dict:
        """The Area Set Cache (`area_sets.json.gz`): `area_set_id` -> `advisor.area_sets.AreaSet`."""
        return area_sets.from_json(self._read("area_sets.json.gz", area_sets.AREA_SETS_VERSION))

    def save_area_sets(self, cache: Mapping) -> None:
        self._write("area_sets.json.gz", area_sets.to_json(cache))

    def append_history(self, record: Mapping[str, Any]) -> None:
        """Add one History Record. The file is rewritten atomically, so a crash never leaves a
        torn last line."""
        path = self.folder / HISTORY_FILE
        existing = path.read_bytes() if path.exists() else b""
        line = json.dumps(strip_credentials(dict(record)), sort_keys=True) + "\n"
        self._write_bytes(HISTORY_FILE, existing + gzip.compress(line.encode("utf-8"), mtime=0))

    def load_history(self) -> list[dict]:
        """Every History Record, oldest first (lines that can't be read are skipped)."""
        try:
            text = gzip.decompress((self.folder / HISTORY_FILE).read_bytes()).decode("utf-8")
        except (OSError, EOFError, ValueError):
            return []
        records = []
        for line in text.splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def _read(self, name: str, version: int) -> dict | None:
        path = self.folder / name
        try:
            data = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("version") == version else None

    def _write(self, name: str, data: dict) -> None:
        self._write_bytes(name, gzip.compress(json.dumps(strip_credentials(data)).encode("utf-8"), mtime=0))

    def _write_bytes(self, name: str, payload: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.folder / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def merge_definitions(cached: Definitions | None, response: Mapping[str, Any]) -> Definitions:
    """The cache updated with a `getdefinitions` response (full, or a delta of changed tables)."""
    tables = dict(cached.tables) if cached else {}
    tables.update({k: v for k, v in response.items() if "_defines" in k and isinstance(v, list)})
    checksum = response.get("checksum")
    return Definitions(str(checksum) if checksum is not None else (cached.checksum if cached else None), tables)


def _kept(body: Any) -> Any:
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k not in _NOT_KEPT and not k.startswith("debug_")}
    return strip_credentials(body)
