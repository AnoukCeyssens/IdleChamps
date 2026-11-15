"""Next Steps' Push tab and its Ready to complete block (#36), through the HTTP app.

Recording facts used: Grand Tour (campaign 1) holds The Mad Wizard (7, done at area 50) and its
variant Pure Energy (9, best 84 of 100); Feast of the Moon (campaign 5) is the Running Event, with
The Crypt of Legends (Morte) (2036) done at area 419 and the other champions' Crypts (none owned)
not run yet; Liar's Night isn't running. Every Push estimate there anchors on Kas's BUD 1.53e124
(area ~484 on the event curve, ~419 on the core curve), so the Likely ones are those whose Area
Set (6 for Grand Tour, 12 for the Crypts) is in the recording and that carry no modifier the
estimate can't handle.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows

PUSH = "/next-steps?goal=Push"


def _app_on(tmp_path, change=lambda raw: None) -> TestClient:
    raw = read_responses(FIXTURE_LOG)
    change(raw)
    log = tmp_path / "requestlog.log"
    write_responses(raw, log)
    return TestClient(create_app(log))


def _focus(page: str) -> list[str]:
    return [r["text"] for r in rows(page, "focus-adventure")]


def test_push_tab_defaults_to_the_furthest_unfinished_adventure_per_campaign_i_am_progressing(client):
    page = client.get(PUSH).text

    assert "Push" in [r["text"] for r in rows(page, "next-steps-tab")]
    # Grand Tour: Pure Energy (best 84 of 100). Feast of the Moon: Morte is done and no other
    # adventure has been run, so the first unfinished one. Liar's Night isn't running.
    assert _focus(page) == ["Pure Energy Unpin", "The Crypt of Legends (Gromma) Unpin"]


def test_push_steps_are_ranked_by_push_gain_across_the_focus_set(tmp_path):
    from test_hints import HINT_TABLES
    from test_next_steps import CELESTE_FEAT, EVELYN, SPURT_CHEST, SPURT_CHEST_2

    def add_tables(raw):
        raw["getdefinitions"] = {**raw["getdefinitions"], **HINT_TABLES}

    page = _app_on(tmp_path, add_tables).get("/next-steps/steps", params={"goal": "Push"}).text
    reasons = [r["text"] for r in rows(page, "step-reason")]

    # Push scores Main DPS damage: the worked Gold Farm gains of test_hints without the ^0.332
    # (Evelyn ×4, Spurt's chest 1.1446^(1/0.332) = ×1.50, ...), the same on both Focus adventures.
    assert [r.split(" → +")[0] for r in reasons[:4]] == [EVELYN, SPURT_CHEST, CELESTE_FEAT, SPURT_CHEST_2]
    assert [r["text"] for r in rows(page, "step-gain")][:4] == [
        "+300% on average", "+50% on average", "+50% on average", "+33% on average"
    ]
    assert reasons[0].startswith(
        f"{EVELYN} → +300% Main DPS damage on Pure Energy, +300% on The Crypt of Legends (Gromma) "
    )


def _ready(client) -> list[str]:
    return [r["text"] for r in rows(client.get("/next-steps/ready").text, "ready-adventure")]


CRYPTS = [
    "The Crypt of Legends (Gromma)", "The Crypt of Legends (Vlahnya)", "The Crypt of Legends (Sentry)",
    "The Crypt of Legends (Penelope)", "The Crypt of Legends (Widdle)", "The Crypt of Legends (Virgil)",
    "The Crypt of Legends (Jang Sao)", "The Crypt of Legends (Minthara)", "The Crypt of Legends (Skylla)",
]


def test_ready_to_complete_lists_only_likely_uncompleted_adventures_i_can_start_now(client):
    ready = _ready(client)

    assert set(ready) == {*CRYPTS, "Pure Energy", "Skittering Skulls"}
    # Left out: Brightly into Darkness (Borderline: its Area Set isn't known), Cacophony of Curses
    # (no estimate for its modifiers), Morte's Crypt and The Mad Wizard (done), Towering
    # Expectations (locked: needs an adventure not completed), Liar's Night (not running).


def test_an_event_adventure_is_ready_only_while_its_event_runs(tmp_path):
    def end_the_event(raw):
        raw["getuserdetails"]["details"]["events_details"]["active_events"] = []

    assert _ready(_app_on(tmp_path, end_the_event)) == ["Pure Energy"]


def test_ready_to_complete_is_ordered_by_what_completing_unlocks_then_campaign_order(client):
    page = client.get("/next-steps/ready").text

    # Each Crypt unlocks its three variants and its (unowned) event champion; Pure Energy and
    # Skittering Skulls unlock nothing new, Grand Tour (free play) before Feast of the Moon.
    assert [r["text"] for r in rows(page, "ready-adventure")] == [*CRYPTS, "Pure Energy", "Skittering Skulls"]
    assert rows(page, "ready-unlocks")[0]["text"] == (
        "Unlocks variant Respectful Restraint, variant The Relentless Undead, variant A Grand Nandventure, "
        "variant Free Play (Gromma), champion Gromma"
    )
    assert rows(page, "ready-verdict")[0]["text"].startswith("Likely: ~")


def _own(raw, champion_id: int) -> None:
    heroes = raw["getuserdetails"]["details"]["heroes"]
    heroes[:] = [h for h in heroes if int(h["hero_id"]) != champion_id]
    heroes.append({"hero_id": str(champion_id), "owned": "1", "level": "100"})


def test_a_patron_unlock_ranks_first_and_an_owned_champion_unlocks_nothing(tmp_path):
    def change(raw):
        # Hand-written `patron_defines` (shape assumed: an adventure-style requirement list).
        mirt = {"id": 1, "name": "Mirt", "requirements": [{"condition": "adventure_complete", "adventure_id": 948}]}
        raw["getdefinitions"] = {**raw["getdefinitions"], "patron_defines": [mirt]}
        _own(raw, 16)  # Gromma: her Crypt now only unlocks its variants

    ready = _ready(_app_on(tmp_path, change))

    widdle, gromma = "The Crypt of Legends (Widdle)", "The Crypt of Legends (Gromma)"
    others = [c for c in CRYPTS if c not in (widdle, gromma)]
    assert ready == [widdle, *others, gromma, "Pure Energy", "Skittering Skulls"]


def _with_mirt(completed_patron_adventure_ids):
    def change(raw):
        patrons = raw["getuserdetails"]["details"]["patrons"]
        next(p for p in patrons if p["patron_id"] == "1")["unlocked"] = True
        # Hand-built getcampaigndetails (shape from #9); the element shape of patron completion is unverified.
        raw["getcampaigndetails"] = {"success": True, "campaigns": [
            {"campaign_id": "1", "completed_adventure_ids": [], "completed_patron_adventure_ids": completed_patron_adventure_ids},
        ]}
    return change


@pytest.mark.parametrize("completed", [[], [9], ["9_1"], [[1, 9]], {"1": "?"}, [{"adventure": 9}]])
def test_an_unreadable_patron_completion_counts_as_not_completed_for_ready_to_complete(tmp_path, completed):
    client = _app_on(tmp_path, _with_mirt(completed))

    ready = _ready(client)
    assert "Pure Energy (Mirt)" in ready  # objective 250 under Mirt: Likely
    assert ready.index("Pure Energy (Mirt)") == ready.index("Pure Energy") + 1
    # The Formation view parses the same snapshot without error.
    assert client.get("/adventures/9?goal=Push&patron=1").status_code == 200


@pytest.mark.parametrize("completed", [{"1": [9]}, {"1": ["9"], "2": []}, [{"patron_id": "1", "adventure_id": "9"}]])
def test_a_readable_patron_completion_leaves_ready_to_complete(tmp_path, completed):
    ready = _ready(_app_on(tmp_path, _with_mirt(completed)))

    assert "Pure Energy" in ready and "Pure Energy (Mirt)" not in ready


def test_the_push_tab_opens_with_ready_to_complete(client):
    page = client.get(PUSH).text

    assert rows(page, "ready-loading")  # loaded by htmx from /next-steps/ready
    assert not rows(client.get("/next-steps?goal=Gem%20Farm").text, "ready-loading")


# --- live mode: Push steps measured in the background, over the Area Set Cache -------------------


def _small(*without_sets: int) -> dict:
    """The recording's getuserdetails trimmed to Grand Tour's Mad Wizard and Pure Energy (fast to
    measure), without the given Area Sets."""
    from fake_game import user_details

    body = user_details()
    defines = body["defines"]
    defines["adventure_defines"] = [a for a in defines["adventure_defines"] if int(a["id"]) in (7, 9)]
    defines["adventure_area_defines"] = [
        a for a in defines["adventure_area_defines"] if a["area_set_id"] not in without_sets
    ]
    return body


def _push_steps(client) -> str:
    return client.get("/next-steps/steps", params={"goal": "Push"}).text


def test_an_area_set_learnt_by_a_refresh_measures_the_push_steps_again(live):
    from fake_game import STEPS

    live.server.queue("getuserdetails", _small(6), _small())
    client = live.start()
    [first] = live.pools
    assert (9, "Push") in first.jobs(STEPS)  # Pure Energy, the Push Focus Set
    first.run(kind=STEPS)
    assert not rows(_push_steps(client), "steps-measuring")

    live.clock.advance(3600)
    client.post("/refresh")  # brings Area Set 6: same recommendation key, a new Area Set Cache

    [_, second] = live.pools  # the harness fails if it starts before the first is shut down
    assert (9, "Push") in second.jobs(STEPS)
    assert rows(_push_steps(client), "steps-measuring")


def test_next_steps_and_the_recompute_share_one_background_pool(live):
    """Same workers, same data (snapshot, Area Set Cache, Model Statuses): two never run side by side."""
    from fake_game import RECOMPUTE, STEPS

    live.server.queue("getuserdetails", _small())
    client = live.start()

    [pool] = live.pools
    assert pool.jobs(RECOMPUTE) and pool.jobs(STEPS)
    pool.run()
    assert pool.shut_down  # idle: nothing left
    assert not rows(_push_steps(client), "steps-measuring")


def live_statuses(client, ids=None) -> dict:
    """The app's own Model Statuses (champion id -> status), read back from the champion pages."""
    from advisor.champion_models import ModelStatus
    from test_model_status import INSTANCE_2_FORMATION

    statuses = {}
    for champion_id in ids if ids is not None else INSTANCE_2_FORMATION:
        (row,) = rows(client.get(f"/champions/{champion_id}").text, "model-status")
        statuses[champion_id] = next(s for s in ModelStatus if row["text"] == f"Model Status: {s.value}")
    return statuses
