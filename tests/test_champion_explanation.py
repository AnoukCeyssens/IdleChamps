"""Champion page, "how the advisor uses it" (#37): each gear slot, feat and specialization with its
modelled effect at my gear (the ChampionExplanation), where the champion is seated, and its Next Steps.

Seam 1 (the HTTP app). Real gear effects come from the recording (Spurt's loot); feats, abilities
and Celeste's slot 2 come from the hand-written `HINT_TABLES` (`test_hints`), whose worked gains are
reused for the Next Steps.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows


def _app_with(tmp_path, **tables) -> TestClient:
    raw = read_responses(FIXTURE_LOG)
    raw["getdefinitions"] = {**raw["getdefinitions"], **tables}
    log = tmp_path / "requestlog.log"
    write_responses(raw, log)
    return TestClient(create_app(log))


@pytest.fixture
def hinted(tmp_path):
    from test_hints import HINT_TABLES

    return _app_with(tmp_path, **HINT_TABLES)


def test_gear_shows_its_modelled_effect_at_my_item_level(client):
    page = client.get("/champions/43").text  # Spurt: Rare slot 1 at item level 3, Uncommon slot 2 at level 1

    slots = rows(page, "gear-slot")
    # Clingy: 120% at Rare, × (1 + 2/250) at item level 3 = 120.96%.
    assert "With your gear: +121% damage to all champions" in slots[0]["text"]
    assert "With your gear: +65% damage to all champions" in slots[1]["text"]


def test_feats_specializations_and_buffing_gear_show_their_modelled_effect(hinted):
    celeste = hinted.get("/champions/2").text
    # Slot 2 (Rare, item level 5) raises Blessing by 50% × (1 + 4/250) = 50.8%.
    assert "With your gear: +50.8% to Blessing" in rows(celeste, "gear-slot")[1]["text"]
    feat = next(r for r in rows(celeste, "feat") if r["text"].startswith("Inspiring Presence"))
    assert "With your gear: +50% damage to all champions" in feat["text"]

    bruenor = hinted.get("/champions/1").text
    options = {r["text"].split(" · ")[0].split()[0]: r["text"] for r in rows(bruenor, "spec-option")}
    assert "With your gear: +50% damage to the adjacent champions ahead" in options["Vanguard"]
    assert "With your gear: +50% damage to the adjacent champions behind" in options["Rearguard"]


def test_a_seated_champion_lists_where_it_is_seated_with_its_choices(hinted):
    page = hinted.get("/champions/2").text  # Celeste
    assert rows(page, "seated") == []
    assert "No recommendation computed so far seats Celeste" in page

    hinted.get("/adventures/9?goal=Gold%20Farm")  # Pure Energy: opening the tab computes it
    hinted.get("/adventures/9?goal=Push")

    page = hinted.get("/champions/2").text
    seated = [r["text"] for r in rows(page, "seated")]
    assert seated == [
        "Pure Energy · Push · Slot 2 · +251% to Arkhan · Feats: Feat 11",
        "Pure Energy · Gold Farm · Slot 2 · +251% to Arkhan · Feats: Feat 11",
    ]
    assert "2 of 372 adventure × Run Goal recommendations computed so far" in page
    assert rows(page, "seated-reason") == []  # the same choices for both Run Goals


def test_choices_that_differ_per_run_goal_give_the_reason(tmp_path):
    from test_hints import HINT_TABLES

    # Kas (3 feat slots) owns four feats: three damage feats and a gold one. Push takes the three
    # damage feats, Gold Farm trades the smallest for gold.
    feats = [
        {"id": 1929, "hero_id": 153, "name": "Bloodthirst", "description": "", "effects": [{"effect_string": "global_dps_multiplier_mult,100"}]},
        {"id": 1931, "hero_id": 153, "name": "Cruelty", "description": "", "effects": [{"effect_string": "global_dps_multiplier_mult,50"}]},
        {"id": 1933, "hero_id": 153, "name": "Spite", "description": "", "effects": [{"effect_string": "global_dps_multiplier_mult,20"}]},
        {"id": 1935, "hero_id": 153, "name": "Greed", "description": "", "effects": [{"effect_string": "gold_multiplier_mult,100"}]},
    ]
    app = _app_with(tmp_path, **{**HINT_TABLES, "hero_feat_defines": [*HINT_TABLES["hero_feat_defines"], *feats]})
    app.get("/adventures/9?goal=Gold%20Farm")
    app.get("/adventures/9?goal=Push")

    page = app.get("/champions/153").text
    seated = {r["text"].split(" · ")[1]: r["text"] for r in rows(page, "seated")}
    assert "Feats: Bloodthirst, Cruelty, Spite" in seated["Push"]
    assert "Feats: Bloodthirst, Cruelty, Greed" in seated["Gold Farm"]
    reasons = [r["text"] for r in rows(page, "seated-reason")]
    assert reasons == [
        "Push picks Spite (+20% damage to all champions) for Main DPS damage",
        "Gold Farm picks Greed (+100% gold) for Favor per hour",
    ]


def test_the_champion_page_loads_its_next_steps_per_run_goal(hinted):
    page = hinted.get("/champions/2").text
    assert [r["text"] for r in rows(page, "champion-steps-loading")] == [
        "Push: measuring Next Steps…", "Gem Farm: measuring Next Steps…", "Gold Farm: measuring Next Steps…",
    ]
    for goal in ("Push", "Gem%20Farm"):  # every Next Steps tab, Push (#36) first, answers
        answered = hinted.get(f"/champions/2/next-steps?goal={goal}").text
        assert rows(answered, "champion-step") or rows(answered, "no-champion-steps")
        assert all("Celeste" in r["text"] for r in rows(answered, "champion-step"))

    steps = hinted.get("/champions/2/next-steps?goal=Gold%20Farm").text  # Focus Set: Pure Energy
    found = [r["text"] for r in rows(steps, "champion-step")]
    # The worked gains from test_hints / test_next_steps, Celeste's only.
    assert found[0].startswith("Gold Farm +14% on average · Unlock Celeste's feat Inspiring Presence → +14% Torm's Favor")
    assert all("Celeste" in step for step in found)

    evelyn = hinted.get("/champions/26/next-steps?goal=Gold%20Farm").text  # not owned
    assert [r["text"].split(" → ")[0] for r in rows(evelyn, "champion-step")] == [
        "Gold Farm +58% on average · Get Evelyn (The Great Modron March, Year 1)"
    ]


def test_a_not_modelled_champion_without_steps_says_so(client):
    page = client.get("/champions/153/next-steps?goal=Gold%20Farm").text  # Kas: no generic effects

    assert rows(page, "champion-step") == []
    assert [r["text"] for r in rows(page, "no-champion-steps")] == [
        "No Next Step for Kas raises Gold Farm on its Focus Set ."
    ]


def test_effects_the_model_cannot_compute_say_not_modelled_never_a_number(tmp_path, client):
    # Recorded Kas: his specializations' abilities aren't in the recording.
    kas = rows(client.get("/champions/153").text, "spec-option")
    assert all("Not modelled" in option["text"] and "With your gear" not in option["text"] for option in kas)

    # With his abilities loaded (hand-written): a champion-unique key and an expression.
    app = _app_with(tmp_path, effect_defines=[
        {"id": 2085, "effect_keys": [{"effect_string": "kas_bloody_handed,100"}]},
        {"id": 2086, "effect_keys": [{"effect_string": "hero_dps_multiplier_mult,10", "amount_expr": "stacks*10"}]},
        {"id": 2087, "effect_keys": [{"effect_string": "hero_dps_multiplier_mult,200"}]},
    ])
    options = {r["text"].split(" · ")[0]: r for r in rows(app.get("/champions/153").text, "spec-option")}
    assert "Not modelled: kas_bloody_handed is not a generic effect." in options["Kas the Bloody Handed"]["text"]
    assert "Not modelled: hero_dps_multiplier_mult depends on amount_expr." in options["Kas the Betrayer"]["text"]
    assert "With your gear: +200% damage to Kas" in options["Kas the Destroyer"]["text"]
