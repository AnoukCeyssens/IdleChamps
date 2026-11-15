"""History Records: one slim, credential-free line per successful Refresh in `history.jsonl.gz`."""

from __future__ import annotations

import gzip
import json

import pytest
import uvicorn
from fastapi.testclient import TestClient

from advisor.cli import main
from conftest import FIXTURE_LOG
from fake_game import SECRETS, unreachable, user_details


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


def _history(live) -> list[dict]:
    path = live.data_dir / "history.jsonl.gz"
    if not path.exists():
        return []
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode("utf-8").splitlines()]


def test_a_successful_refresh_writes_a_history_record(live):
    live.start()

    [record] = _history(live)
    assert record["version"] == 1
    assert record["server_time"] == 1794732215
    assert record["game_version"] == "1.242.1 (v659)"  # the newest changelog entry; the client sent v659


def _later(seconds: int, change=lambda details: None) -> dict:
    """getuserdetails `seconds` later on the server, with an optional change to the account."""
    body = user_details()
    body["current_time"] += seconds
    for instance in body["details"]["game_instances"]:
        instance["seconds_since_reset"] += seconds
    change(body["details"])
    return body


def _refresh_again(live, client, body: dict) -> None:
    live.server.queue("getuserdetails", body)
    live.clock.advance(61)
    client.post("/refresh")


def test_a_refresh_where_only_the_time_changed_writes_no_record(live):
    client = live.start()

    _refresh_again(live, client, _later(60))

    assert live.server.calls().count("getuserdetails") == 2
    assert len(_history(live)) == 1


def test_a_refresh_with_progress_writes_another_record(live):
    client = live.start()

    _refresh_again(live, client, _later(60, lambda d: d.update(red_rubies=1000)))

    first, second = _history(live)
    assert second["server_time"] == first["server_time"] + 60
    assert (first["totals"]["red_rubies"], second["totals"]["red_rubies"]) == (961, 1000)


def test_a_failed_refresh_writes_no_record(live):
    live.server.queue("getpatrondetails", *[unreachable()] * 10)

    live.start()

    assert _history(live) == []


def test_a_failed_refresh_after_a_good_one_adds_nothing(live):
    client = live.start()
    live.server.queue("getuserdetails", _later(60, lambda d: d.update(red_rubies=1000)))
    live.server.queue("getpatrondetails", *[unreachable()] * 10)
    live.clock.advance(61)

    client.post("/refresh")

    assert len(_history(live)) == 1


def test_offline_mode_writes_no_history(served, tmp_path):
    data_dir = tmp_path / "data"

    main(["--from-log", str(FIXTURE_LOG), "--data-dir", str(data_dir)])
    [(app, _)] = served
    with TestClient(app) as client:
        client.get("/champions")

    assert not (data_dir / "history.jsonl.gz").exists()


def _level_up(champion_id: str, level: str):
    def change(details):
        next(h for h in details["heroes"] if h["hero_id"] == champion_id)["level"] = level

    return change


def test_the_first_record_holds_the_owned_state_and_its_recommendation_key(live):
    live.start()

    [record] = _history(live)
    assert record["recommendation_key"]
    owned = record["owned_state"]
    assert len(owned["champions"]) == 32  # owned, of the 168 in the catalogue
    assert owned["champions"]["1"]["level"] == 300  # Bruenor
    assert owned["modron_cores"] == {"1": 1941}


def test_a_currency_only_change_keeps_the_key_and_leaves_the_owned_state_out(live):
    client = live.start()

    _refresh_again(live, client, _later(60, lambda d: d.update(red_rubies=1000)))

    first, second = _history(live)
    assert second["recommendation_key"] == first["recommendation_key"]
    assert "owned_state" not in second


def test_a_changed_champion_level_changes_the_key_and_stores_the_owned_state_again(live):
    client = live.start()

    _refresh_again(live, client, _later(60, _level_up("1", "310")))

    first, second = _history(live)
    assert second["recommendation_key"] != first["recommendation_key"]
    assert second["owned_state"]["champions"]["1"]["level"] == 310



def test_a_changed_modron_reset_area_changes_the_key(live):
    client = live.start()

    _refresh_again(live, client, _later(60, lambda d: d["modron_saves"]["1"].update(area_goal="350")))

    first, second = _history(live)
    assert second["recommendation_key"] != first["recommendation_key"]  # it shapes the Gem Farm result
    assert second["owned_state"]["modron_saves"]["1"]["area_goal"] == 350

def test_the_owned_state_returns_once_the_key_changes_again(live):
    client = live.start()
    _refresh_again(live, client, _later(60, lambda d: d.update(red_rubies=1000)))

    _refresh_again(live, client, _later(120, lambda d: (d.update(red_rubies=1000), _level_up("1", "310")(d))))

    assert ["owned_state" in r for r in _history(live)] == [True, False, True]


def test_records_never_hold_credentials_shop_data_or_definitions(live):
    client = live.start()
    _refresh_again(live, client, _later(60, _level_up("1", "310")))

    text = gzip.decompress((live.data_dir / "history.jsonl.gz").read_bytes()).decode("utf-8")
    for secret in SECRETS:
        assert secret not in text
    for field in ("user_id", "hash", "instance_id", "defines", "current_sales", "package_deals", "promotions"):
        assert f'"{field}"' not in text
    assert "_defines" not in text


def test_the_record_holds_account_totals_and_each_running_games_run(live):
    live.start()

    [record] = _history(live)
    assert record["definitions_checksum"] == "1727461343"
    totals = record["totals"]
    assert totals["red_rubies"] == 961
    assert totals["red_rubies_spent"] == 10000
    assert totals["contracts"] == {"31": 5, "32": 31, "33": 13}
    assert totals["chests"] == {"1": 14}
    assert totals["resets"] == 31
    assert totals["seconds_played"] == 328753
    assert totals["highest_areas"]["2036"] > 0
    first, second = record["instances"]
    assert first["game_instance_id"] == 1
    assert first["adventure_id"] == 2036
    assert (first["current_area"], first["highest_area"]) == (419, 420)
    assert first["run_started_at"] == 1794732215 - 162528
    assert first["gems_earned"] == 161
    assert first["top_damage_champion_id"] == 153
    assert first["formation"] == [56, 158, 126, 163, 128, 153, 59, 149, 25, 43]
    assert first["push_estimate"]["objective"] == 50
    assert second["adventure_id"] == 9
    assert second["formation"] == [3, 1, 6, 7, 2, 5, 4, 13, -1]
    # The Push estimate for the formation in play (worked out in test_push.py): Asharra's own run anchors it.
    assert second["push_estimate"] == {
        "expected_area": 85, "area": 84, "band": 34, "objective": 100, "verdict": "Borderline",
    }


def test_a_history_record_that_cant_be_built_leaves_the_refresh_successful(live, caplog):
    from conftest import rows

    body = user_details()
    body["details"]["reset_currencies"] = [{"total_earned": 5}]  # a currency without its id: the record can't be built
    live.server.queue("getuserdetails", body)

    page = live.start().get("/champions").text

    assert _history(live) == []
    assert not rows(page, "refresh-failed")
    assert rows(page, "refresh-state")[0]["text"] == "refreshed 0 s ago"
    assert rows(page, "champion")  # the snapshot was loaded
    assert "History Record" in caplog.text
