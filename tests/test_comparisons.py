"""Community and saved formation comparisons, model review flags for community Gem Farm gaps, and
Push calibration flags (#39).

Recording facts used: one saved formation, "New Formation Save" (formation-save campaign 100003, the
Feast of the Moon adventures such as The Crypt of Legends (Morte), 2036), seating Havilar, Kalix,
Hank, Lae'zel, Kas, Baeloth, Duke Ravengard, Catti-brie and Spurt; all owned. The account owns none
of the Briv Gem Farm champions (Briv, Hew Maan, Thellora, Diana, Widdle, Ellywick, BBEG) that
Emmote's Grand Tour formations seat. Instance 2 runs Pure Energy (9) at highest area 85; its Push
estimate is area 84 ± 34.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from advisor.account import parse_account
from advisor.catalogue import parse_catalogue
from advisor.champion_models import ChampionModels
from advisor.recommendation import recommend
from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows

CRYPT = 2036  # The Crypt of Legends (Morte), Feast of the Moon
MAD_WIZARD = 7  # Grand Tour of the Sword Coast
PIN = "0fde7dff186290ba57d038dedfcf0d5bc74b8aa9"  # Emmotes/ic_scripting_routes, vendored
HUGE_BLESSING = {
    "upgrade_defines": [{"id": 90101, "hero_id": 2, "required_level": 10, "effect": "effect_def,90101", "name": "Huge Blessing"}],
    "effect_defines": [{"id": 90101, "effect_keys": [{"effect_string": "hero_dps_multiplier_mult,1e40", "targets": ["adj"]}]}],
}
BRIV_FARM = {58: "Briv", 75: "Hew Maan", 139: "Thellora", 148: "Diana", 91: "Widdle", 83: "Ellywick", 125: "BBEG", 99: "Dungeon Master"}


def _app(tmp_path_factory, name, change) -> TestClient:
    responses = read_responses(FIXTURE_LOG)
    change(responses)
    log = tmp_path_factory.mktemp(name) / "requestlog.log.gz"
    write_responses(responses, log)
    return TestClient(create_app(log))


def _grant(responses, champions=BRIV_FARM, level=500):
    """Own the Briv Gem Farm champions; give Celeste (owned, in none of the community formations) a
    huge generic buff to adjacent champions (×10^38, about 120 more areas killed at once on Gem
    Farm), and make the best area on The Mad Wizard 20, where the Gem Farm model anchors. The
    recording has no effect definitions: without one every Gem Farm formation scores alike."""
    responses["getdefinitions"] = {**responses["getdefinitions"], **HUGE_BLESSING}
    responses["getuserdetails"]["details"]["stats"]["highest_area_completed_ever_c7"] = "20"
    heroes = responses["getuserdetails"]["details"]["heroes"]
    for champion_id in champions:
        heroes.append({
            "hero_id": str(champion_id), "level": str(level), "owned": "1", "feat_slots": 0,
            "specialization_choices": [], "unlocked_feats": [], "active_feats": [],
        })


def _comparisons(page: str) -> list[str]:
    return [r["text"] for r in rows(page, "comparison")]


# --- Seam 1: the comparison lines on the Formation view -------------------------------------------


@pytest.mark.parametrize("goal", ["Gold Farm", "Gem Farm", "Push"])
def test_the_saved_formation_is_compared_on_every_run_goal_of_its_adventures(client, goal):
    page = client.get(f"/adventures/{CRYPT}", params={"goal": goal}).text

    [saved] = [c for c in _comparisons(page) if c.startswith("Saved")]
    assert saved.startswith("Saved formation “New Formation Save”: ")
    # It seeds the search too, so the recommendation is never weaker than it.
    match = re.search(r": (\d+)% weaker by our model|: as strong as the recommendation", saved)
    assert match, saved
    if match.group(1) and int(match.group(1)) > 0:
        assert 1 <= len(rows(page, "comparison-effect")) <= 2 * len(_comparisons(page))


def test_a_saved_formation_is_not_compared_on_adventures_of_other_formation_save_campaigns(client):
    page = client.get(f"/adventures/{MAD_WIZARD}", params={"goal": "Gold Farm"}).text

    assert not [c for c in _comparisons(page) if c.startswith("Saved")]


def test_community_gem_farm_formations_are_listed_with_their_pinned_source_and_what_is_missing(client):
    page = client.get(f"/adventures/{MAD_WIZARD}", params={"goal": "Gem Farm"}).text

    community = [c for c in _comparisons(page) if c.startswith("Community")]
    # Emmote's Grand Tour entries: BBEG with 2, 3 or 4 champions next to Widdle (Q formation).
    assert [c.split("”")[0] for c in community] == [
        f"Community formation “Grand Tour of the Sword Coast · BBEG · {n} next to Widdle · Q formation" for n in (2, 3, 4)
    ]
    assert all("not comparable: needs " in c and "Briv" in c and "(not owned)" in c for c in community)
    links = re.findall(r'href="([^"]+)" data-row="comparison-source"', page)
    assert len(links) == 3 and all(PIN in link for link in links)


def test_community_formations_only_match_their_run_goal_and_campaign(client):
    gold = client.get(f"/adventures/{MAD_WIZARD}", params={"goal": "Gold Farm"}).text
    crypt = client.get(f"/adventures/{CRYPT}", params={"goal": "Gem Farm"}).text

    assert not [c for c in _comparisons(gold) if c.startswith("Community")]
    assert not [c for c in _comparisons(crypt) if c.startswith("Community")]


@pytest.fixture(scope="module")
def briv_farmer(tmp_path_factory) -> TestClient:
    """The recording plus the Briv Gem Farm champions, at level 500."""
    return _app(tmp_path_factory, "briv_farmer", _grant)


def test_an_owned_community_formation_shows_how_much_weaker_it_is_and_why(briv_farmer):
    page = briv_farmer.get(f"/adventures/{MAD_WIZARD}", params={"goal": "Gem Farm"}).text

    community = [r["text"] for r in rows(page, "comparison") if r["text"].startswith("Community")]
    assert len(community) == 3
    assert all(re.search(r"Q formation”: (\d+% weaker by our model|as strong as the recommendation)", c) for c in community)
    effects = [r["text"] for r in rows(page, "comparison-effect")]
    assert effects and all(re.search(r"Main DPS .+ instead of |is not in this formation| adds ", e) for e in effects)


# --- Seam 2: curated and saved formations seed the search and never beat it -----------------------


def _domain(change=lambda r: None):
    raw = read_responses(FIXTURE_LOG)
    change(raw)
    catalogue, account = parse_catalogue(raw), parse_account(raw)
    return catalogue, account, ChampionModels(catalogue, account)


def test_the_recommendation_is_never_weaker_than_a_legal_community_or_saved_formation():
    catalogue, account, models = _domain(_grant)
    for adventure_id, goal in ((MAD_WIZARD, "Gem Farm"), (CRYPT, "Gold Farm"), (CRYPT, "Push")):
        rec = recommend(catalogue, account, catalogue.adventure(adventure_id), goal, models=models)
        compared = [c for c in rec.comparisons if c.pct_weaker is not None]
        assert compared, (adventure_id, goal)
        assert all(c.pct_weaker >= -1e-6 for c in compared)
        assert all(len(c.top_effects) <= 2 for c in compared)


# --- Model review flag: a community Gem Farm formation far below the recommendation ---------------


def test_a_community_gem_farm_formation_more_than_10_pct_below_raises_a_model_review_flag(briv_farmer):
    page = briv_farmer.get(f"/adventures/{MAD_WIZARD}", params={"goal": "Gem Farm"}).text
    gaps = [int(m) for m in re.findall(r"Q formation”: (\d+)% weaker", page)]
    assert gaps and max(gaps) > 10  # the generic model knows nothing of Briv's skips or BBEG

    status = briv_farmer.get("/model-status").text

    flagged = [r["text"] for r in rows(status, "model-flag") if r["text"].startswith("Community Gem Farm formation")]
    assert len(flagged) == sum(1 for g in gaps if g > 10)
    assert all("The Mad Wizard" in f and "% below the recommendation" in f for f in flagged)


def test_no_model_review_flag_for_community_formations_on_the_recording(client):
    client.get(f"/adventures/{MAD_WIZARD}", params={"goal": "Gem Farm"})  # none of them owned: not comparable

    status = client.get("/model-status").text

    assert not [r for r in rows(status, "model-flag") if r["text"].startswith("Community")]


# --- Push calibration flags -------------------------------------------------------------------------


def _instance(responses, instance_id):
    return next(i for i in responses["getuserdetails"]["details"]["game_instances"] if int(i["game_instance_id"]) == instance_id)


def test_no_push_calibration_flags_on_the_recording(client):
    page = client.get("/model-status").text

    # Instance 1 reached 420 against an estimate of 459 ± 15; instance 2 is at the boss of area 85,
    # its estimated wall (84 ± 10), and its best area there is 84.
    assert not rows(page, "calibration-flag")
    assert rows(page, "no-calibration-flags")


@pytest.fixture(scope="module")
def beyond_the_band(tmp_path_factory) -> TestClient:
    """Instance 2 (Pure Energy, estimate 84 ± 10) reached area 130."""
    return _app(tmp_path_factory, "beyond_the_band", lambda r: _instance(r, 2).update(highest_area="130"))


def test_a_run_beating_its_estimate_by_more_than_the_band_raises_a_push_calibration_flag(beyond_the_band):
    page = beyond_the_band.get("/model-status").text

    [flag] = [r["text"] for r in rows(page, "calibration-flag")]
    assert flag == (
        "Instance 2 on Pure Energy reached area 130: beyond the Push estimate for its formation (area 84 ± 10) by "
        "46 areas, more than the band width."
    )


@pytest.fixture(scope="module")
def best_beyond_the_band(tmp_path_factory) -> TestClient:
    """Pure Energy's best area raised to 130 (story 98: "my best area beats the estimate"); instance 2's
    own run is still at area 85."""

    def raise_best(responses):
        responses["getuserdetails"]["details"]["stats"]["highest_area_completed_ever_c9"] = 130

    return _app(tmp_path_factory, "best_beyond_the_band", raise_best)


def test_a_best_area_beating_the_estimate_by_more_than_the_band_raises_a_push_calibration_flag(best_beyond_the_band):
    page = best_beyond_the_band.get("/model-status").text

    [flag] = [r["text"] for r in rows(page, "calibration-flag")]
    assert flag == (
        "Instance 2 on Pure Energy has a best area of 130 there: beyond the Push estimate for its formation (area 84 ± 10) by "
        "46 areas, more than the band width."
    )


@pytest.fixture(scope="module")
def kas_alone(tmp_path_factory) -> TestClient:
    """Instance 1 (The Crypt of Legends (Morte), area 420) with only Kas, its BUD champion, in play: one
    champion hitting every 5 s needs 250 s for the 50-hit Resurrected Hero of the Vale at area 20."""

    def alone(responses):
        instance = _instance(responses, 1)
        instance["formation"] = [153 if c == 153 else -1 for c in instance["formation"]]

    return _app(tmp_path_factory, "kas_alone", alone)


def test_a_run_past_a_boss_the_model_said_it_cannot_break_raises_a_push_calibration_flag(kas_alone):
    page = kas_alone.get("/model-status").text

    flags = [r["text"] for r in rows(page, "calibration-flag")]
    assert (
        "Instance 1 on The Crypt of Legends (Morte) reached area 420: it passed the boss at area 20, which the model "
        "said its formation can't break in time."
    ) in flags
