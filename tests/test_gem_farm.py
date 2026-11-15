"""Gem Farm (#29): gems per hour, familiars and the modron core, through `recommend` (Seam 2) and the
Formation view (Seam 1).

The recording holds 3 familiars (ids 1, 113, 119) and one modron core (core 1, 1941 experience,
area goal 500, 10 nodes placed). Its best area on The Mad Wizard (adventure 7) is 50.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from advisor.account import parse_account
from advisor.catalogue import parse_catalogue
from advisor.recommendation import recommend
from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows
from test_recommendation import account, catalogue, modelled, raw  # noqa: F401 (shared fixtures)


def test_gem_farm_is_scored_in_gems_per_hour_with_a_recommended_reset_area(modelled):
    catalogue, account = modelled
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gem Farm")

    assert rec.infeasible_reason is None
    assert rec.score_label == "Gems per hour"
    assert 0 < rec.score < 10_000
    assert rec.modron.core_id == 1
    assert rec.modron.reset_area % 5 == 0
    # Best area 50: the formation stops killing at once soon after, so it resets around there.
    assert 40 <= rec.modron.reset_area <= 70
    assert rec.modron.reset_area_optimised


def _seated(rec):
    return [s for s in rec.seats if s.champion is not None]


def _only(raw, hero_ids):
    responses = dict(raw)
    details = dict(raw["getuserdetails"]["details"])
    details["heroes"] = [h for h in details["heroes"] if int(h["hero_id"]) in hero_ids]
    responses["getuserdetails"] = {**raw["getuserdetails"], "details": details}
    return parse_catalogue(responses), parse_account(responses)


def test_a_stronger_formation_resets_no_earlier_and_earns_more(catalogue, account, modelled):
    plain = recommend(catalogue, account, catalogue.adventure(7), "Gem Farm")
    buffed = recommend(*modelled, modelled[0].adventure(7), "Gem Farm")  # adds +200% and +50% to Arkhan

    assert buffed.modron.reset_area >= plain.modron.reset_area
    assert buffed.score > plain.score


def test_every_familiar_is_placed_on_a_seated_champion_or_on_clicks(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gem Farm")

    assert sorted(f.familiar_id for f in rec.familiars) == [1, 113, 119]
    on_field = [f for f in rec.familiars if f.slot is not None]
    seated = {s.slot: s for s in _seated(rec)}
    assert len({f.slot for f in on_field}) == len(on_field)
    for f in on_field:
        assert seated[f.slot].champion == f.champion
        assert seated[f.slot].familiar_id == f.familiar_id


@pytest.mark.parametrize("goal", ["Gem Farm", "Gold Farm"])
def test_familiars_without_a_champion_to_sit_on_go_on_clicks(raw, goal):
    catalogue, account = _only(raw, {12})  # Arkhan only

    rec = recommend(catalogue, account, catalogue.adventure(7), goal)

    assert len([f for f in rec.familiars if f.slot is not None]) <= 1
    assert len([f for f in rec.familiars if f.slot is None]) >= 2


def test_gold_farm_gains_familiars_and_the_modron_core_with_its_saved_reset_area(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")

    assert len(rec.familiars) == 3
    assert rec.familiars[0].champion == rec.main_dps.champion  # Main DPS first
    assert rec.modron.core_id == 1 and rec.modron.experience == 1941
    assert rec.modron.reset_area == 500 and not rec.modron.reset_area_optimised
    assert rec.modron.nodes == 10


def test_gem_farm_states_its_scoring_assumptions(catalogue, account):
    text = " ".join(recommend(catalogue, account, catalogue.adventure(7), "Gem Farm").scoring_assumptions)

    assert "expected count at the reset area" in text
    assert "expected value" in text
    assert "Briv" in text
    assert "don't count as hits for breaking" in text
    assert "ignored for Gem Farm" in text


# --- the Formation view (Seam 1) ------------------------------------------------------------------

GEM = "/adventures/{}?goal=Gem%20Farm"


def test_gem_farm_tab_shows_gems_per_hour_and_the_modron_core(client):
    page = client.get(GEM.format(7)).text

    (score,) = rows(page, "score")
    assert score["text"].startswith("Score ") and score["text"].endswith("Gems per hour")
    (core,) = rows(page, "modron-core")
    assert core["text"] == "Core 1 · 1941 experience · 10 nodes"
    (reset,) = rows(page, "modron-reset-area")
    assert re.fullmatch(r"Reset area \d+ \(recommended\)", reset["text"])
    assert len(rows(page, "modron-node-row")) == 3  # the saved layout, trimmed to its nodes
    assumptions = " ".join(r["text"] for r in rows(page, "assumption"))
    assert "expected count at the reset area" in assumptions


def test_familiars_show_on_the_grid_and_in_the_right_rail(client):
    page = client.get(GEM.format(7)).text

    familiars = [r["text"] for r in rows(page, "familiar")]
    assert len(familiars) == 3
    assert re.fullmatch(r"Familiar 1: slot \d+ \(Arkhan\)", familiars[0])  # on the Main DPS
    on_grid = [s["text"] for s in rows(page, "seat") if "familiar" in s["classes"]]
    assert len(on_grid) == len([f for f in familiars if not f.endswith("on clicks")])
    assert any("Arkhan" in s and "Familiar 1" in s for s in on_grid)


def test_familiars_left_on_clicks_are_listed(tmp_path_factory):
    responses = read_responses(FIXTURE_LOG)
    details = dict(responses["getuserdetails"]["details"])
    details["heroes"] = [h for h in details["heroes"] if int(h["hero_id"]) == 12]  # Arkhan only
    responses["getuserdetails"] = {**responses["getuserdetails"], "details": details}
    log = tmp_path_factory.mktemp("arkhan") / "requestlog.log.gz"
    write_responses(responses, log)

    page = TestClient(create_app(log)).get(GEM.format(7)).text

    familiars = [r["text"] for r in rows(page, "familiar")]
    assert sum(1 for f in familiars if f.endswith(": on clicks")) >= 2


def test_gold_farm_shows_the_saved_reset_area(client):
    (reset,) = rows(client.get("/adventures/7?goal=Gold%20Farm").text, "modron-reset-area")

    assert reset["text"] == "Reset area 500 (as saved)"
