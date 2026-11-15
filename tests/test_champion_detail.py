"""Champion detail: what I own of a champion (gear, feats, specializations) with the game's own text."""

import re

from conftest import FIXTURE_LOG, rows
from fastapi.testclient import TestClient

from advisor.request_log import read_responses, write_responses
from advisor.web import create_app


def test_each_champion_in_the_list_links_to_its_detail_page(client):
    page = client.get("/champions").text

    assert len(re.findall(r'href="/champions/\d+"', page)) == 168
    assert 'href="/champions/153"' in page  # Kas

    detail = client.get("/champions/153")
    assert detail.status_code == 200
    assert "<h1>Kas</h1>" in detail.text


def test_owned_champion_lists_each_specialization_choice_point_with_its_options(client):
    page = client.get("/champions/153").text  # Kas, level 1160, chose Kas the Bloody Handed

    choices = rows(page, "spec-choice")
    assert len(choices) == 1
    assert "Level 180" in choices[0]["text"]

    options = {row["text"].split(" · ")[0]: row for row in rows(page, "spec-option")}
    assert list(options) == ["Kas the Bloody Handed", "Kas the Betrayer", "Kas the Destroyer"]
    assert "chosen" in options["Kas the Bloody Handed"]["classes"]
    assert "chosen" not in options["Kas the Betrayer"]["classes"]
    assert "Betraying those in charge is second nature for someone like Kas." in options["Kas the Betrayer"]["text"]


def test_owned_champion_shows_gear_rarity_and_level_per_slot_with_game_text(client):
    page = client.get("/champions/43").text  # Spurt: gear in three of six slots

    slots = rows(page, "gear-slot")
    assert len(slots) == 6
    assert "Slot 1" in slots[0]["text"]
    assert "Clingy" in slots[0]["text"] and "Rare" in slots[0]["text"] and "Level 3" in slots[0]["text"]
    assert "Trust me. No hugs." in slots[0]["text"]
    assert "Rot Grub Grenade" in slots[1]["text"] and "Uncommon" in slots[1]["text"] and "Level 1" in slots[1]["text"]
    assert "Whoops." in slots[1]["text"]
    assert "Basket of Centipedes" in slots[2]["text"]
    assert all("No gear" in slot["text"] for slot in slots[3:])
    assert "empty" in slots[5]["classes"]


def test_owned_champion_shows_feats_owned_and_equipped(client):
    page = client.get("/champions/153").text  # Kas: four feats owned, three equipped in three slots

    assert "3 feat slots" in page
    feats = rows(page, "feat")
    assert len(feats) == 4
    assert sum("equipped" in feat["classes"] for feat in feats) == 3
    assert all("owned" in feat["classes"] for feat in feats)


def test_feats_show_the_game_text_and_the_ones_not_yet_owned(tmp_path):
    # The recorded Game Definitions are a delta without `hero_feat_defines`, so this test adds a
    # small hand-written table for Kas in the shape the game publishes (id, hero_id, name,
    # description, effects). Kas owns 1929, 1931, 1933, 1935; 1937 is one he doesn't have.
    feat_defines = [
        {"id": 1929, "hero_id": 153, "name": "Bloodthirst", "description": "Increases Kas' damage by 20%.",
         "effects": [{"effect_string": "hero_dps_multiplier_mult,20,153"}]},
        {"id": 1937, "hero_id": 153, "name": "Vampiric Lord", "description": "Increases Kas' health by 30%.",
         "effects": [{"effect_string": "health_mult,30"}]},
    ]
    page = _client_with_definitions(tmp_path, hero_feat_defines=feat_defines).get("/champions/153").text

    feats = {feat["text"].split(" · ")[0]: feat for feat in rows(page, "feat")}
    assert "Increases Kas' damage by 20%." in feats["Bloodthirst"]["text"]
    assert "equipped" in feats["Bloodthirst"]["classes"]
    assert "Not owned" in feats["Vampiric Lord"]["text"]
    assert "owned" not in feats["Vampiric Lord"]["classes"]
    assert "Increases Kas' health by 30%." in feats["Vampiric Lord"]["text"]
    assert len(feats) == 5  # the three owned feats without a loaded definition are still listed


def test_unowned_champion_shows_its_source_and_game_text_but_no_owned_state(client):
    page = client.get("/champions/181").text  # Morte, not owned

    assert "<h1>Morte</h1>" in page
    assert "Not owned" in page
    assert "Feast of the Moon, Year 10" in page
    assert rows(page, "gear-slot") == []
    assert rows(page, "feat") == []

    choices = rows(page, "spec-choice")
    assert [c["text"].split()[:2] for c in choices] == [["Level", "130"], ["Level", "180"]]
    options = rows(page, "spec-option")
    assert len(options) == 6
    assert not any("chosen" in option["classes"] for option in options)
    assert any("Morte cackles at the enemy's pathetic attempts" in option["text"] for option in options)


def test_unknown_champion_is_not_found(client):
    assert client.get("/champions/99999").status_code == 404


def _client_with_definitions(tmp_path, **tables) -> TestClient:
    """The app over the recorded fixture, with extra Game Definitions tables added to getdefinitions."""
    responses = read_responses(FIXTURE_LOG)
    responses["getdefinitions"] = {**responses["getdefinitions"], **tables}
    log = tmp_path / "requestlog.log.gz"
    write_responses(responses, log)
    return TestClient(create_app(log))
