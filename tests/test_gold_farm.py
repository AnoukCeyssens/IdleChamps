"""Gold Farm tab (#28): the first Formation Recommendation, through the HTTP app.

`modelled` adds the small hand-written ability table from test_recommendation.py to the recording
(which has no `effect_defines`), so contributions to the Main DPS are visible.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows
from test_recommendation import EFFECT_TABLES

GOLD = "/adventures/{}?goal=Gold%20Farm"


def _app(tmp_path_factory, name, change) -> TestClient:
    responses = read_responses(FIXTURE_LOG)
    change(responses)
    log = tmp_path_factory.mktemp(name) / "requestlog.log.gz"
    write_responses(responses, log)
    return TestClient(create_app(log))


@pytest.fixture(scope="module")
def modelled(tmp_path_factory) -> TestClient:
    def add_tables(responses):
        responses["getdefinitions"] = {**responses["getdefinitions"], **EFFECT_TABLES}

    return _app(tmp_path_factory, "modelled", add_tables)


def _one(page: str, kind: str) -> str:
    (row,) = rows(page, kind)
    return row["text"]


def _seat_rows(page: str) -> dict[str, str]:
    """Seats table rows by champion name (rows read "Slot <n> <champion> Level <n> ...")."""
    found = {}
    for row in rows(page, "seat-row"):
        if match := re.match(r"Slot \d+ (.+?) (Level|Lent)", row["text"]):
            found[match.group(1)] = row["text"]
    return found


def test_gold_farm_shows_the_main_dps_with_its_bud(modelled):
    page = modelled.get(GOLD.format(7)).text

    main = _one(page, "main-dps")
    assert main.startswith("Main DPS: Arkhan")
    assert "BUD" in main


def test_every_other_seat_is_labelled_by_its_contribution_to_the_main_dps(modelled):
    page = modelled.get(GOLD.format(7)).text

    seats = _seat_rows(page)
    assert "+200% to Arkhan" in seats["Celeste"]  # adjacent buff, raised by Big Blessing
    assert "+0% to Arkhan · +200% gold" in seats["Jarlaxle"]
    assert "Main DPS" in seats["Arkhan"]
    # The grid carries the same labels.
    grid = [s["text"] for s in rows(page, "seat")]
    assert any("Celeste" in s and "+200% to Arkhan" in s for s in grid)


def test_seats_table_lists_slot_champion_level_specialization_and_feats(modelled):
    page = modelled.get(GOLD.format(7)).text

    seats = _seat_rows(page)
    assert len(seats) == 9
    bruenor = seats["Bruenor"]
    assert bruenor.startswith("Slot ")
    assert "Level 300" in bruenor
    assert "Vanguard" in bruenor or "Rearguard" in bruenor
    assert "Level 1160" in seats["Kas"]
    # Kas: the feats equipped in his three slots are kept (no feat definitions to choose by).
    assert "Feat" in seats["Kas"]


def test_score_and_gap_are_shown(modelled):
    page = modelled.get(GOLD.format(7)).text

    assert _one(page, "score").startswith("Score ")
    assert "Torm's Favor per hour (relative)" in _one(page, "score")
    assert _one(page, "gap") == "at most 0% from best"


def test_scoring_assumptions_include_click_and_daily_cap_ones(modelled):
    assumptions = [r["text"] for r in rows(modelled.get(GOLD.format(7)).text, "assumption")]

    assert any("click" in a.lower() for a in assumptions)
    assert any("daily" in a.lower() for a in assumptions)


def test_unverified_champions_are_badged(modelled):
    page = modelled.get(GOLD.format(7)).text

    assert _one(page, "unverified-badge") == "9 champions unverified, score may be off"


def test_left_out_lists_owned_champions_not_seated_with_reasons(modelled):
    left = [r["text"] for r in rows(modelled.get(GOLD.format(2024)).text, "left-out")]

    assert "Bruenor: not allowed: only Evil champions" in left


def test_escort_blocked_slots_are_marked_in_the_seats_table(client):
    page = client.get(GOLD.format(289)).text  # escorts on slots 0 and 2

    blocked = [r["text"] for r in rows(page, "seat-row") if "blocked" in r["classes"]]
    assert blocked == ["Slot 0 blocked by an escort", "Slot 2 blocked by an escort"]
    assert [s["text"] for s in rows(page, "seat") if "blocked" in s["classes"]] == [
        "Slot 0: blocked by an escort Slot 0 · blocked",
        "Slot 2: blocked by an escort Slot 2 · blocked",
    ]


def test_patron_chip_switches_to_that_patron_variant(client):
    plain = client.get(GOLD.format(9)).text
    vajra = client.get(GOLD.format(9) + "&patron=2").text  # Vajra: CON 14+ only

    vajra_left = [r["text"] for r in rows(vajra, "left-out")]
    assert "Asharra: not allowed by Vajra: only champions with CON of at least 14" in vajra_left
    assert not any("Vajra" in r["text"] for r in rows(plain, "left-out"))
    assert "Asharra" not in " ".join(_seat_rows(vajra))


def test_no_legal_formation_shows_the_missing_piece(tmp_path_factory):
    def only_dps(responses):
        details = dict(responses["getuserdetails"]["details"])
        details["heroes"] = [h for h in details["heroes"] if int(h["hero_id"]) in (12, 153)]  # Arkhan, Kas
        responses["getuserdetails"] = {**responses["getuserdetails"], "details": details}

    page = _app(tmp_path_factory, "only-dps", only_dps).get(GOLD.format(361)).text  # DPS's Day Off

    assert _one(page, "infeasible") == (
        "No legal formation: needs at least 1 champion that fits “no DPS champions”, and none of yours do."
    )
    assert rows(page, "main-dps") == []


def test_other_run_goals_do_not_show_a_recommendation_yet(client):
    assert rows(client.get("/adventures/7?goal=Gem%20Farm").text, "main-dps") == []


# --- "What would make this better" hints (#34) -----------------------------------------------


@pytest.fixture(scope="module")
def hinted(tmp_path_factory) -> TestClient:
    from test_hints import HINT_TABLES

    def add_tables(responses):
        responses["getdefinitions"] = {**responses["getdefinitions"], **HINT_TABLES}

    return _app(tmp_path_factory, "hinted", add_tables)


def test_opening_a_recommendation_loads_its_hints_with_numeric_gains(hinted):
    page = hinted.get(GOLD.format(7)).text
    (loader,) = re.findall(r'data-row="hints-loading" hx-get="([^"]+)" hx-trigger="load"', page)

    hints = [r["text"] for r in rows(hinted.get(loader.replace("&amp;", "&")).text, "hint")]

    assert hints[:2] == [
        "Get Evelyn (The Great Modron March, Year 1) → +58% Torm's Favor per hour",
        "Use a Potion of the Giant's Strength → +26% Torm's Favor per hour",
    ]
    assert "Blacksmith contract on Celeste's slot 2 (item level 5 → 29) → +0.5% Torm's Favor per hour" in hints
    assert all(re.search(r" → \+[\d.]+% Torm's Favor per hour$", h) for h in hints)
    assert not any("Potion of Speed" in h for h in hints)  # no modelled gain: not shown


def test_potions_stay_out_of_the_baseline_score(hinted, modelled):
    # Only Evil may play here, so Celeste (whose gear the hinted tables add) sits out: the two apps
    # differ by potions alone, and the score must not.
    page = hinted.get(GOLD.format(2024)).text
    assert _one(page, "score") == _one(modelled.get(GOLD.format(2024)).text, "score")
    assert "No potions." in [r["text"] for r in rows(page, "assumption")]


def test_push_recommendations_load_hints_too(hinted):
    page = hinted.get("/adventures/7?goal=Push").text
    (loader,) = re.findall(r'data-row="hints-loading" hx-get="([^"]+)" hx-trigger="load"', page)

    hints = [r["text"] for r in rows(hinted.get(loader.replace("&amp;", "&")).text, "hint")]

    assert hints[0] == "Get Evelyn (The Great Modron March, Year 1) → +300% Main DPS damage"
    assert all(re.search(r" → \+[\d.]+% Main DPS damage$", h) for h in hints)
