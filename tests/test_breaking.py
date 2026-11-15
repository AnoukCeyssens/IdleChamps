"""Area Set Cache and Armoured / Hits-based bosses in the Push search and verdict (#33).

Recording facts used: `getuserdetails.defines.adventure_area_defines` holds Area Sets 6 (The Mad
Wizard, Pure Energy) and 12 (the Crypt of Legends event adventures). Set 12's area-20 boss is the
hits-based Resurrected Hero of the Vale (monster 220, 50 hits); set 6 has no armoured or hits-based
monster. Pure Energy's estimate (~419) is far above its objective 100.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from advisor.account import parse_account
from advisor.catalogue import parse_catalogue
from advisor.champion_models import ChampionModels, Seat
from advisor.push_verdict import push_verdict
from advisor.recommendation import recommend
from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows

PUSH = "/adventures/{}?goal=Push"


def _one(page: str, kind: str) -> str:
    (row,) = rows(page, kind)
    return row["text"]


def _assumptions(page: str) -> list[str]:
    return [r["text"] for r in rows(page, "assumption")]


def _app(tmp_path_factory, name, change) -> TestClient:
    responses = read_responses(FIXTURE_LOG)
    change(responses)
    log = tmp_path_factory.mktemp(name) / "requestlog.log.gz"
    write_responses(responses, log)
    return TestClient(create_app(log))


def _drop_area_set(responses, area_set_id):
    defines = responses["getuserdetails"]["defines"]
    defines["adventure_area_defines"] = [
        a for a in defines["adventure_area_defines"] if a["area_set_id"] != area_set_id
    ]


def test_known_placement_lifts_the_borderline_cap(client):
    page = client.get(PUSH.format(9)).text

    assert _one(page, "push-verdict") == "Likely"
    assert not rows(page, "push-cap")
    assert not rows(page, "placement-hint")
    assert any(a.startswith("Armoured and hits-based bosses checked") and "Area Set 6" in a for a in _assumptions(page))


@pytest.fixture(scope="module")
def no_set_6(tmp_path_factory) -> TestClient:
    return _app(tmp_path_factory, "no_set_6", lambda r: _drop_area_set(r, 6))


def test_unknown_placement_caps_and_hints_to_load_an_adventure_of_the_same_area_set(no_set_6):
    page = no_set_6.get(PUSH.format(9)).text

    assert _one(page, "push-verdict") == "Borderline"
    assert rows(page, "push-cap")
    assert _one(page, "placement-hint") == "Load Pure Energy in-game, then Refresh"


@pytest.fixture(scope="module")
def pure_energy_locked(tmp_path_factory) -> TestClient:
    """Set 6 unknown, and Pure Energy neither played nor unlocked (it needs an adventure never completed)."""

    def lock(responses):
        _drop_area_set(responses, 6)
        details = responses["getuserdetails"]["details"]
        details["stats"].pop("highest_area_completed_ever_c9", None)
        details["game_instances"] = [i for i in details["game_instances"] if int(i.get("current_adventure_id") or 0) != 9]
        for define in responses["getuserdetails"]["defines"]["adventure_defines"]:
            if define["id"] == 9:
                define["requirements"] = [{"condition": "adventure_complete", "adventure_id": 2040}]

    return _app(tmp_path_factory, "pure_energy_locked", lock)


def test_the_hint_names_an_unlocked_adventure_of_the_same_area_set(pure_energy_locked):
    page = pure_energy_locked.get(PUSH.format(9)).text

    assert _one(page, "placement-hint") == "Load The Mad Wizard in-game, then Refresh"


# --- Seam 2: verdicts for formations the search wouldn't pick ------------------------------------


def _domain(change=lambda r: None):
    raw = read_responses(FIXTURE_LOG)
    change(raw)
    catalogue, account = parse_catalogue(raw), parse_account(raw)
    return catalogue, account, ChampionModels(catalogue, account)


def _main_alone(catalogue, account, models, adventure_id):
    """The verdict for the recommended Main DPS on its own, and the recommendation."""
    adventure = catalogue.adventure(adventure_id)
    rec = recommend(catalogue, account, adventure, "Push", models=models)
    seat = Seat(rec.main_dps.champion.id, rec.main_dps.slot)
    return push_verdict(catalogue, account, models, adventure, None, (seat,), seat), rec


def test_a_hits_based_boss_the_formation_cannot_break_in_120_s_ends_the_estimate():
    catalogue, account, models = _domain()

    # Free Play (Morte), Area Set 12: the Resurrected Hero of the Vale (50 hits) at area 20. One
    # champion hitting every 5 s (cooldown not loaded) needs 250 s.
    alone, rec = _main_alone(catalogue, account, models, 2040)

    assert alone.area == 19 and alone.expected_area == 20
    assert (
        "Breaking: Resurrected Hero of the Vale at area 20 (hits-based, 50 hits) takes 250 s to break, over the "
        "120 s limit, so the estimate stops at area 19." in alone.assumptions
    )
    # The recommended ten champions hit twice a second: 25 s.
    assert rec.push_verdict.area > 400
    assert not any(a.startswith("Breaking:") for a in rec.push_verdict.assumptions)


@pytest.mark.parametrize("keep", ["tag", "flag"])
def test_hits_based_bosses_are_found_by_their_flag_or_their_tag(keep):
    def only(raw):
        for monster in raw["getuserdetails"]["defines"]["monster_defines"]:
            if monster["id"] == 220:
                if keep == "tag":
                    monster["properties"] = {}
                else:
                    monster["tags"] = ["boss", "undead"]

    alone, _ = _main_alone(*_domain(only), 2040)

    assert alone.area == 19


# --- Armoured bosses and Breakers ---------------------------------------------------------------

ARKHAN, BRUENOR = 12, 1


def _armoured_pure_energy(raw, *, chunks=25):
    """Every boss of Pure Energy armoured with `chunks` chunks (a variant's `set_monster_health_type`)."""
    for define in raw["getuserdetails"]["defines"]["adventure_defines"]:
        if define["id"] == 9:
            define["game_changes"] = [
                *(define.get("game_changes") or ()),
                {"type": "set_monster_health_type", "health_type": "armor_based", "amount": chunks,
                 "target_class": "boss"},
            ]


def _definitions(raw, **tables):
    raw["getdefinitions"] = {**raw["getdefinitions"], **tables}


def _breaker(raw):
    """Bruenor gains a generic Breaker effect: +1 chunk per hit for every champion."""
    _definitions(
        raw,
        upgrade_defines=[{"id": 91001, "hero_id": BRUENOR, "required_level": 10, "effect": "effect_def,91001",
                          "name": "Shield Breaker"}],
        effect_defines=[{"id": 91001, "effect_keys": [
            {"effect_string": "increase_damage_against_monster_armor,1", "targets": ["all"]}]}],
    )


def test_an_armoured_boss_the_main_dps_cannot_break_in_120_s_ends_the_estimate():
    catalogue, account, models = _domain(_armoured_pure_energy)

    rec = recommend(catalogue, account, catalogue.adventure(9), "Push", models=models)

    # 25 chunks, one chunk per Main DPS hit every 5 s: 125 s, so the first boss (area 5) stalls the run.
    assert rec.main_dps.champion.id == ARKHAN
    assert BRUENOR not in {s.champion.id for s in rec.seats if s.champion}
    assert rec.push_verdict.area == 4
    assert rec.push_verdict.label == "Unlikely"
    assert any("(armoured, 25 chunks) takes 125 s to break" in a for a in rec.push_verdict.assumptions)


def test_the_search_seats_a_breaker_where_armoured_bosses_need_one():
    def with_breaker(raw):
        _armoured_pure_energy(raw)
        _breaker(raw)

    catalogue, account, models = _domain(with_breaker)
    rec = recommend(catalogue, account, catalogue.adventure(9), "Push", models=models)

    # Two chunks per Main DPS hit: 62.5 s.
    assert BRUENOR in {s.champion.id for s in rec.seats if s.champion}
    assert not any(a.startswith("Breaking:") for a in rec.push_verdict.assumptions)
    assert rec.push_verdict.label == "Likely"


def test_faster_attacks_break_armour_sooner():
    def fast_arkhan(raw):
        _armoured_pure_energy(raw)
        _definitions(raw, attack_defines=[{"id": 25, "cooldown": 2}])  # Arkhan's base attack

    catalogue, account, models = _domain(fast_arkhan)
    rec = recommend(catalogue, account, catalogue.adventure(9), "Push", models=models)

    assert rec.main_dps.champion.id == ARKHAN
    assert rec.push_verdict.label == "Likely"  # 25 × 2 s = 50 s at area 5
    # Far up, the chunks outgrow the Main DPS's single hits (15 hits of 2 s still kill a boss there).
    (stop,) = [a for a in rec.push_verdict.assumptions if a.startswith("Breaking:")]
    assert "no hit reaches its chunk health" in stop


def test_damage_over_time_ultimates_count_as_hits_on_hits_based_bosses():
    def dot_ultimate(raw):  # Arkhan's ultimate: a 30 s pool ticking every 0.6 s
        _definitions(raw, attack_defines=[{"id": 26, "cooldown": 300, "pool_time": 30, "pool_interval": 0.6}])

    alone, _ = _main_alone(*_domain(dot_ultimate), 2040)

    # 0.2 hits/s from the base attack + 50 ticks once in 120 s: 50 / (0.2 + 0.4167) = 81 s.
    assert not any(a.startswith("Breaking:") for a in alone.assumptions)
    assert alone.area > 19
