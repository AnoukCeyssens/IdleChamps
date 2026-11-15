"""The Store: the app data folder that live Refreshes write and later sessions start from."""

from __future__ import annotations

import gzip
import json
import os

from conftest import rows
from fake_game import SECRETS

def _persisted(live) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in live.data_dir.rglob("*") if p.is_file() and p.name != "config.toml"}


def test_refresh_writes_gzipped_versioned_json(live):
    live.start()

    files = _persisted(live)
    assert set(files) == {"definitions.json.gz", "snapshot.json.gz", "history.jsonl.gz"}
    for data in files.values():
        for line in gzip.decompress(data).decode("utf-8").splitlines():  # history: one JSON per line
            assert json.loads(line)["version"] == 1


def test_no_credential_appears_in_any_persisted_file(live):
    client = live.start()
    live.server.queue("getpatrondetails", {"success": False, "failure_reason": "Outdated instance id"})
    live.clock.advance(61)
    page = client.post("/refresh").text  # a second Refresh, through a failure response, rewrites the files
    assert rows(page, "refresh-failed") == []

    for name, data in _persisted(live).items():
        text = gzip.decompress(data).decode("utf-8")
        for secret in SECRETS:
            assert secret not in text, name
        for field in ("user_id", "hash", "instance_id", "linked_accounts"):
            assert f'"{field}"' not in text, name
        assert "current_sales" not in text, name  # shop and promo data aren't kept either


def test_a_new_session_starts_from_the_cached_snapshot(live):
    live.start()
    live.clock.advance(300)
    live.log.write_text("")  # this time the startup Refresh fails

    page = live.start().get("/champions").text

    assert len(rows(page, "champion")) == 168
    assert rows(page, "refresh-state")[0]["text"] == "refreshed 5 min ago"
    assert rows(page, "refresh-failed") != []


def test_a_failed_write_leaves_the_previous_files_intact(live, monkeypatch):
    client = live.start()
    before = _persisted(live)

    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    live.clock.advance(61)
    page = client.post("/refresh").text

    assert _persisted(live) == before  # no half-written file, no leftover temp file
    [banner] = rows(page, "refresh-failed")
    assert "data folder" in banner["text"]
    assert len(rows(page, "champion")) == 168


def test_cooldown_comes_from_the_config_file(live):
    with (live.data_dir / "config.toml").open("a") as config:
        config.write("cooldown_seconds = 10\n")
    client = live.start()

    assert rows(client.get("/champions").text, "refresh-button")[0]["text"] == "Refresh in 10 s"
    live.clock.advance(10)
    assert rows(client.get("/champions").text, "refresh-button")[0]["text"] == "Refresh"
