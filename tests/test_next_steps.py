"""Next Steps (#35): the Gem Farm and Gold Farm tabs, and the Focus Set they are measured on.

Seam 1 (the HTTP app). Ranking and reason tests run offline on the recording plus `HINT_TABLES`
from `test_hints` (hand-written effects, feats and gear: the recording has none). Expected gains
are the worked values from `test_hints` (Evelyn +58.45%, Spurt's chest +14.46%, ...), on one Focus
adventure or averaged by hand over two.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows

GEM, GOLD = "/next-steps?goal=Gem%20Farm", "/next-steps?goal=Gold%20Farm"


def _focus(page: str) -> list[str]:
    return [r["text"] for r in rows(page, "focus-adventure")]


def test_default_focus_sets(client):
    # Gem Farm: no instance farms with a modron reset, so the last-played adventure (the active
    # instance's), from the Running Event, Feast of the Moon.
    assert _focus(client.get(GEM).text) == ["The Crypt of Legends (Morte) Unpin"]
    # Gold Farm: Torm's Favor is the only Favor earned (Grand Tour); an instance is on Pure Energy.
    assert _focus(client.get(GOLD).text) == ["Pure Energy Unpin"]


def _app_on(tmp_path, change=lambda raw: None) -> TestClient:
    """The app in offline mode on the recording with `change` made to its responses."""
    raw = read_responses(FIXTURE_LOG)
    change(raw)
    log = tmp_path / "requestlog.log"
    write_responses(raw, log)
    return TestClient(create_app(log))


def test_gem_farm_defaults_to_the_instances_that_farm(tmp_path):
    def farm_grand_tour(raw):
        core = raw["getuserdetails"]["details"]["modron_saves"]["1"]
        core["formation_saves"] = {"1": 3}  # Grand Tour, where instance 2 runs Pure Energy
        core["properties"]["toggle_preferences"]["reset"] = True  # resets at its area goal, 500

    client = _app_on(tmp_path, farm_grand_tour)

    assert _focus(client.get(GEM).text) == ["Pure Energy Unpin"]


@pytest.fixture
def hinted(tmp_path):
    """The recording plus hand-written effects, feats and gear (see `test_hints.HINT_TABLES`)."""
    from test_hints import HINT_TABLES

    def add_tables(raw):
        raw["getdefinitions"] = {**raw["getdefinitions"], **HINT_TABLES}

    return _app_on(tmp_path, add_tables)


def _steps(client, goal="Gold Farm", all=False) -> str:
    return client.get("/next-steps/steps", params={"goal": goal, "all": all}).text


def _ranked(page: str) -> list[str]:
    return [r["text"] for r in rows(page, "step-gain")]


def _step(page: str, action: str) -> dict:
    return next(r for r in rows(page, "step") if action in r["text"])


EVELYN = "Get Evelyn (The Great Modron March, Year 1)"
SPURT_CHEST = "Open a chest for Spurt's slot 1 (Rare → Epic)"
SPURT_CHEST_2 = "Open a chest for Spurt's slot 2 (Uncommon → Rare)"
CELESTE_FEAT = "Unlock Celeste's feat Inspiring Presence"


def test_steps_are_ranked_by_gain_on_the_focus_set(hinted):
    page = _steps(hinted)
    reasons = [r["text"] for r in rows(page, "step-reason")]

    # The worked gains from test_hints, on Pure Energy alone.
    assert [r.split(" → +")[0] for r in reasons[:4]] == [EVELYN, SPURT_CHEST, CELESTE_FEAT, SPURT_CHEST_2]
    assert _ranked(page)[:4] == ["+58% on average", "+14% on average", "+14% on average", "+10% on average"]


def test_the_reason_follows_the_template_and_names_its_scoring_assumption(hinted):
    reason = rows(_steps(hinted), "step-reason")[0]["text"]

    assert reason.startswith(
        f"{EVELYN} → +58% Torm's Favor per hour on Pure Energy (Evelyn in slot 4: +300% to Arkhan). "
        "Scoring Assumption: Favor per hour is compared as gold multipliers × Main DPS damage^0.332"
    )


def test_pinning_from_the_header_averages_over_the_focus_set(hinted):
    # Lieutenant of Evil (Liar's Night, not running) only allows evil champions: Evelyn and
    # Celeste can't help there, Spurt's chests help as much as on Pure Energy.
    header = hinted.get("/adventures/1526?goal=Gold%20Farm").text
    assert "Pin to Gold Farm Focus Set" in [r["text"] for r in rows(header, "focus-toggle")]

    hinted.post("/adventures/1526/focus?goal=Gold%20Farm&pin=true")

    assert _focus(hinted.get(GOLD).text) == ["Pure Energy Unpin", "Lieutenant of Evil (event not running) Unpin"]
    header = hinted.get("/adventures/1526?goal=Gold%20Farm").text
    assert "Unpin from Gold Farm Focus Set" in [r["text"] for r in rows(header, "focus-toggle")]
    page = _steps(hinted)
    reasons = [r["text"] for r in rows(page, "step-reason")]
    # Evelyn 58.45 / 2; Spurt's chests 14.46 and 10.02 on both; Celeste's feat 14.40 / 2.
    assert [r.split(" → +")[0] for r in reasons[:4]] == [EVELYN, SPURT_CHEST, SPURT_CHEST_2, CELESTE_FEAT]
    assert _ranked(page)[:4] == ["+29% on average", "+14% on average", "+10% on average", "+7.2% on average"]
    assert reasons[1].startswith(
        f"{SPURT_CHEST} → +14% Torm's Favor per hour on Pure Energy, "
        "+14% Leira's Favor per hour on Lieutenant of Evil (event not running) ("
    )


def test_unpinning_from_the_next_steps_tab(hinted):
    hinted.post("/adventures/1526/focus?goal=Gold%20Farm&pin=true")

    hinted.post("/adventures/9/focus?goal=Gold%20Farm&pin=false")

    assert _focus(hinted.get(GOLD).text) == ["Lieutenant of Evil (event not running) Unpin"]
    assert EVELYN not in _steps(hinted)  # it only helped on Pure Energy


def test_top_ten_then_show_all_and_never_a_zero_gain(hinted):
    # The Chaotic Decisionist (Donaar forced in front) gains from many unowned champions.
    hinted.post("/adventures/273/focus?goal=Gold%20Farm&pin=true")
    top = _steps(hinted)
    everything = _steps(hinted, all=True)

    assert len(rows(top, "step")) == 10
    assert len(rows(everything, "step")) > 10
    assert rows(top, "show-all")[0]["text"] == f"Show all {len(rows(everything, 'step'))} steps"
    assert not rows(everything, "show-all")
    assert all(not g.startswith(("+0% ", "+0.00% ")) for g in _ranked(everything))
    assert "Potion" not in everything  # potions are hints only, never Next Steps


def test_unverified_badge_unowned_source_and_cost(hinted):
    page = _steps(hinted)
    evelyn = _step(page, EVELYN)
    contract = _step(_steps(hinted, all=True), "Blacksmith contract on Spurt's slot 1")

    # Every champion is Not Modelled until #38/#40: badged, still ranked first.
    assert "unverified champion" in evelyn["text"]
    assert "Source: The Great Modron March, Year 1 · can't be obtained right now (event not running)" in evelyn["text"]
    # The recording holds no Large Blacksmithing Contracts (buff 34).
    assert "Cost: needs 1 Large Blacksmithing Contract (24 item levels), you have 0" in contract["text"]


# --- live mode: measured in the background, Focus Sets persisted -------------------------------

KEPT = (7, 9, 44, 45)  # adventures kept in the trimmed snapshot (the last-played one, 2036, is not)


def _small() -> dict:
    from fake_game import user_details

    body = user_details()
    defines = body["defines"]
    defines["adventure_defines"] = [a for a in defines["adventure_defines"] if int(a["id"]) in KEPT]
    return body


@pytest.fixture
def live_client(live):
    live.server.queue("getuserdetails", _small())
    return live.start()


def test_focus_adventures_are_measured_in_the_background_after_a_refresh(live, live_client):
    # Gold Farm's and Push's Pure Energy; Gem Farm's last-played adventure isn't in the snapshot.
    [pool] = live.step_pools
    assert sorted(job[2] for job in pool.pending) == [(9, "Gold Farm"), (9, "Push")]

    waiting = _steps(live_client)
    assert rows(waiting, "steps-measuring")[0]["text"].startswith("Measuring in the background: 1 of 1")
    assert not rows(waiting, "step")

    pool.run()
    assert pool.shut_down  # nothing left to measure
    page = _steps(live_client)
    assert not rows(page, "steps-measuring")
    # The recording's own gear effects: Spurt's slot 1 chest, as in the formation's hints.
    assert rows(page, "step-reason")[0]["text"].startswith(f"{SPURT_CHEST} → +14% Torm's Favor per hour on Pure Energy")


def test_a_pinned_focus_set_is_kept_in_the_store_across_restarts(live, live_client):
    live_client.post("/adventures/7/focus?goal=Gold%20Farm&pin=true")
    live_client.post("/adventures/9/focus?goal=Gem%20Farm&pin=true")

    assert len(live.step_pools[0].pending) == 4  # each newly pinned adventure is measured too (+ Push's Pure Energy)
    live.server.queue("getuserdetails", _small())
    restarted = live.start(pools=[])  # a new app on the same data folder (its own recompute)

    assert _focus(restarted.get(GOLD).text) == ["Pure Energy Unpin", "The Mad Wizard Unpin"]
    assert _focus(restarted.get(GEM).text) == ["Pure Energy Unpin"]
