"""Model Status (#38): per-champion status, the live damage check, automatic Stale / Check Failed
transitions, and the Model Status page."""

from __future__ import annotations

import pytest

from advisor import champion_models
from advisor.champion_models import Reconstruction
from conftest import rows
from fake_game import definitions, user_details

# Instance 2 in the recording: Pure Energy (adventure 9), BUD 4.27e27 set by champion 6.
INSTANCE_2_FORMATION = (3, 1, 6, 7, 2, 5, 4, 13)


def _one(page: str, kind: str) -> str:
    [row] = rows(page, kind)
    return row["text"]


# --- offline: the page on the recording -------------------------------------------------------


def test_overview_lists_every_champion_with_its_status_and_links_to_its_page(client):
    page = client.get("/model-status").text

    table = rows(page, "model-status")
    assert len(table) == 168
    bruenor = next(r["text"] for r in table if r["text"].startswith("Bruenor"))
    assert "Not Modelled" in bruenor
    assert 'href="/champions/1"' in page


def test_area_set_coverage_line(client):
    page = client.get("/model-status").text

    # 124 adventures use 8 Area Sets; the recording holds sets 12 (49 adventures) and 6 (2).
    assert _one(page, "placement-coverage") == "Monster placement known for 51 of 124 adventures (2 of 8 area sets)"


def test_offline_mode_records_no_history_and_has_no_data_folder(client):
    page = client.get("/model-status").text

    assert _one(page, "history-line") == "History: not recorded in offline mode"
    assert not rows(page, "data-folder")


def test_no_open_flags_on_the_recording(client):
    page = client.get("/model-status").text

    assert not rows(page, "model-flag")
    assert _one(page, "no-flags") == "No open model review flags."


def test_damage_checks_are_inconclusive_while_a_formation_holds_not_modelled_champions(client):
    page = client.get("/model-status").text

    checks = [r["text"] for r in rows(page, "damage-check")]
    assert len(checks) == 2  # both running instances set a BUD
    assert all("inconclusive" in c for c in checks)
    assert any(c.startswith("Instance 2 · Pure Energy") and "game 4.27e27" in c for c in checks)


def test_model_status_filter_on_the_champions_list(client):
    everyone = client.get("/champions", params={"status": "Not Modelled"}).text
    nobody = client.get("/champions", params={"status": "Verified"}).text

    # 168 champions, 5 of them reconstructed (#40 batch 1: Kas, Briv, Gazrick, Lucius, Hew Maan) and
    # never checked against the game, so Stale.
    assert len(rows(everyone, "champion")) == 163
    assert len(rows(client.get("/champions", params={"status": "Stale"}).text, "champion")) == 5
    assert rows(nobody, "champion") == []
    offered = {r["text"] for r in rows(everyone, "status-filter")}
    assert offered == {"Any status", "Not Modelled", "Verified", "Check Failed", "Stale"}


def test_status_filter_combines_with_the_role_filter(client):
    page = client.get("/champions", params={"role": "dps", "status": "Not Modelled"}).text

    assert len(rows(page, "champion")) == 47  # 49 dps champions, Kas and Lucius reconstructed


def test_champion_page_shows_its_model_status(client):
    page = client.get("/champions/1").text

    assert _one(page, "model-status") == "Model Status: Not Modelled"
    assert [r["text"] for r in rows(page, "model-evidence")] == [
        "Scored on its generic effects only: no reconstruction of its unique effects yet."
    ]


# --- live: transitions over Refreshes ----------------------------------------------------------


@pytest.fixture
def reconstructed(monkeypatch):
    """Every champion of instance 2's formation has a reconstruction with description tests (as #40
    will add), so the live damage check on instance 2 counts."""
    for champion_id in INSTANCE_2_FORMATION:
        monkeypatch.setitem(
            champion_models.RECONSTRUCTIONS, champion_id, Reconstruction((f"test_description_{champion_id}",))
        )


def _status(page: str, name: str) -> str:
    return next(r["text"] for r in rows(page, "model-status") if r["text"].startswith(name + " "))


def _check(page: str, instance: int) -> str:
    return next(r["text"] for r in rows(page, "damage-check") if r["text"].startswith(f"Instance {instance} "))


def _predicted(page: str, instance: int) -> float:
    return float(_check(page, instance).split("model ")[1].split(" vs")[0])


def _refresh(live, client, call: str, body: dict) -> None:
    live.server.queue(call, body)
    live.clock.advance(61)
    client.post("/refresh")


def _with_bud(bud: float) -> dict:
    body = user_details()
    for instance in body["details"]["game_instances"]:
        if int(instance["game_instance_id"]) == 2:
            instance["stats"]["this_reset_highest_damage_dealt"] = repr(bud)
    return body


def _changed_definitions(champion_id: int, **fields) -> dict:
    body = definitions()
    body["checksum"] = 1727461344
    for hero in body["hero_defines"]:
        if hero["id"] == champion_id:
            hero.update(fields)
    return body


def test_failed_damage_check_marks_the_formation_check_failed_and_raises_a_flag(live, reconstructed):
    client = live.start()
    page = client.get("/model-status").text

    # The generic layer is nowhere near the game's 4.27e27: far outside ±2%.
    assert "· failed:" in _check(page, 2)
    assert "inconclusive" in _check(page, 1)  # instance 1 still holds Not Modelled champions
    [flag] = rows(page, "model-flag")
    assert flag["text"].startswith("Live damage check failed on instance 2 (Pure Energy)")
    assert "game 4.27e27" in flag["text"] and "tolerance ±2%" in flag["text"]
    assert _status(page, "Nayeli") == "Nayeli Check Failed"
    assert _status(page, "Bruenor") == "Bruenor Check Failed"
    assert _status(page, "Strix") == "Strix Not Modelled"  # not reconstructed

    champion = client.get("/champions/6").text
    assert _one(champion, "model-status") == "Model Status: Check Failed"
    evidence = [r["text"] for r in rows(champion, "model-evidence")]
    assert evidence[0] == "Description tests: 1 (test_description_6), kept passing by the test suite."
    assert evidence[-1].startswith("2026-11-15: Live damage check on instance 2 (Pure Energy): model ")


def test_a_check_within_two_percent_verifies_and_closes_the_flag(live, reconstructed):
    client = live.start()
    predicted = _predicted(client.get("/model-status").text, 2)

    _refresh(live, client, "getuserdetails", _with_bud(predicted * 1.015))
    page = client.get("/model-status").text

    assert "· passed:" in _check(page, 2)
    assert _status(page, "Nayeli") == "Nayeli Verified"
    assert not rows(page, "model-flag")
    assert len(rows(client.get("/champions", params={"status": "Verified"}).text, "champion")) == 8


def test_a_miss_just_outside_two_percent_fails(live, reconstructed):
    client = live.start()
    predicted = _predicted(client.get("/model-status").text, 2)

    _refresh(live, client, "getuserdetails", _with_bud(predicted * 1.03))

    assert "· failed:" in _check(client.get("/model-status").text, 2)


def test_a_rework_marks_that_champion_stale(live, reconstructed):
    client = live.start()

    _refresh(live, client, "getdefinitions", _changed_definitions(6, last_rework_date="2026-11-01 00:00:00"))
    page = client.get("/model-status").text

    assert _status(page, "Nayeli") == "Nayeli Check Failed"  # untouched
    champion = client.get("/champions/6").text
    assert _one(champion, "model-status") == "Model Status: Stale"
    assert rows(champion, "model-evidence")[-1]["text"].endswith(
        "Reworked (2017-09-07 00:00:00 → 2026-11-01 00:00:00): needs a new live damage check."
    )


def test_a_definitions_change_touching_a_champion_marks_it_stale(live, reconstructed):
    client = live.start()

    _refresh(live, client, "getdefinitions", _changed_definitions(3, base_damage="500"))
    page = client.get("/model-status").text

    assert _status(page, "Nayeli") == "Nayeli Stale"
    assert _status(page, "Bruenor") == "Bruenor Check Failed"


def test_status_and_flags_survive_a_restart(live, reconstructed):
    live.start()

    page = live.start(pools=[]).get("/model-status").text  # a new process

    assert _status(page, "Nayeli") == "Nayeli Check Failed"
    assert len(rows(page, "model-flag")) == 1  # the same observation isn't counted twice


def test_history_line_and_data_folder_button(live, monkeypatch):
    import advisor.web

    opened = []
    monkeypatch.setattr(advisor.web, "open_folder", opened.append)
    client = live.start()
    page = client.get("/model-status").text

    assert _one(page, "history-line") == "History: 1 record since 2026-11-15"
    assert _one(page, "data-folder") == "Open the app data folder"
    client.post("/data-folder")
    assert opened == [live.data_dir]
