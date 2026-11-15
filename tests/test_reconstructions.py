"""Per-champion reconstructions (#40): description tests and tooltip fixtures, through the model's two
interfaces (a formation's score, `ChampionModels.explain`) and `recommend` (Seam 2).

The recording has no `effect_defines` and no definitions for Briv, Gazrick, Lucius or Hew Maan, so
each test adds a small hand-written table in the shapes the Game Definitions publish (effect keys
with `effect_string` and fields; `briv_unnatural_haste`'s parameters are amount, required_stacks,
stack_consume_percent, skip_chance, skip_amount) and, where needed, makes the champion owned.
Expected values are worked out by hand from each champion's description (named in each test).
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import replace
from pathlib import Path

import pytest

from advisor import champion_models
from advisor.hints import candidate_changes, measure, what_if
from advisor.account import parse_account
from advisor.catalogue import parse_catalogue
from advisor.champion_models import RECONSTRUCTIONS, ChampionModels, Loadout, ModelStatus, Seat
from advisor.push_verdict import push_verdict
from advisor.recommendation import recommend
from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from fastapi.testclient import TestClient
from conftest import FIXTURE_LOG, rows
from test_recommendation import DIAMOND

KAS, ARKHAN, BRUENOR, CELESTE = 153, 12, 1, 2
BRIV, GAZRICK, LUCIUS, HEW_MAAN = 58, 98, 72, 75
TOOLTIPS = json.loads((Path(__file__).parent / "fixtures" / "tooltips.json").read_text())


@pytest.fixture(scope="module")
def recorded():
    return read_responses(FIXTURE_LOG)


def _domain(raw, **kw):
    """Catalogue, account and models for the recording plus definitions `tables`, champions `owned`
    (level 1000) with `gear` (hero, slot, rarity, item level), optionally keeping only `heroes_only`."""
    raw = _changed(raw, **kw)
    catalogue, account = parse_catalogue(raw), parse_account(raw)
    return catalogue, account, ChampionModels(catalogue, account)


def _changed(raw, *, tables=None, owned=(), gear=(), heroes_only=None):
    """The recording's responses with those changes (see `_domain`)."""
    raw = copy.deepcopy(raw)
    raw["getdefinitions"] = {**raw["getdefinitions"], **(tables or {})}
    details = raw["getuserdetails"]["details"]
    template = next(h for h in details["heroes"] if int(h["hero_id"]) == BRUENOR)
    for hero_id in owned:
        details["heroes"].append({**template, "hero_id": str(hero_id), "level": "1000", "specialization_choices": [],
                                  "unlocked_feats": [], "active_feats": [], "feat_slots": 0})
    for hero_id, slot, rarity, level in gear:
        details["loot"].append({"hero_id": hero_id, "slot_id": slot, "rarity": rarity, "gild": 0, "enchant": level - 1})
    if heroes_only is not None:
        details["heroes"] = [h for h in details["heroes"] if int(h["hero_id"]) in heroes_only]
    return raw


# --- the framework ---------------------------------------------------------------------------------


def test_every_reconstruction_names_description_tests_that_exist_here():
    here = set(re.findall(r"^def (test_\w+)", Path(__file__).read_text(), re.M))
    assert {BRIV, GAZRICK, LUCIUS, HEW_MAAN, KAS} <= set(RECONSTRUCTIONS)
    for champion_id, reconstruction in RECONSTRUCTIONS.items():
        assert reconstruction.description_tests, champion_id
        for test_id in reconstruction.description_tests:
            path, name = test_id.split("::")
            assert path == "tests/test_reconstructions.py" and name in here, test_id


def test_every_reconstruction_has_tooltip_fixtures():
    covered = {case["champion_id"] for case in TOOLTIPS["cases"]}
    assert set(RECONSTRUCTIONS) <= covered


def test_reconstructed_champions_start_stale_and_others_stay_not_modelled(recorded):
    _, _, models = _domain(recorded)

    assert models.status(KAS) is ModelStatus.STALE  # reconstructed, never checked against the game
    assert models.status(BRUENOR) is ModelStatus.NOT_MODELLED
    assert models.explain(BRUENOR).status is ModelStatus.NOT_MODELLED


# --- Kas -------------------------------------------------------------------------------------------

KAS_TABLES = {"effect_defines": [{"id": 2081, "description": "Kas increases his damage for each Evil Champion "
                                  "in the formation, stacking multiplicatively.",
                                  "effect_keys": [{"effect_string": "hero_dps_multiplier_mult,25",
                                                   "stack_func": "per_hero_attribute", "targets": ["self"]}]}]}


def test_kas_gains_damage_per_evil_champion_stacking_multiplicatively(recorded):
    """Born Into Evil (+25% here): Kas, Arkhan (both Evil) and Bruenor (Good) → ×1.25²."""
    catalogue, account, models = _domain(recorded, tables=KAS_TABLES)
    _, _, generic = _domain(recorded)
    layout = catalogue.adventure(7).layout
    seats = (Seat(KAS, 0), Seat(ARKHAN, 1), Seat(BRUENOR, 2))

    with_kas = models.scorer(layout, "Push", seats[0]).score(seats)
    plain = generic.scorer(layout, "Push", seats[0]).score(seats)

    assert with_kas.bud / plain.bud == pytest.approx(1.25**2)
    assert with_kas.contributions[ARKHAN].damage_pct == pytest.approx(25)
    assert with_kas.contributions[BRUENOR].damage_pct == pytest.approx(0)
    assert models.explain(KAS).value("Born Into Evil per Evil champion") == 25


def test_kas_search_seats_evil_champions_and_its_gap_stays_honest(recorded):
    spurt, kalix, hank = 43, 158, 163  # Spurt and Kalix are Evil, Hank isn't
    catalogue, account, models = _domain(recorded, tables=KAS_TABLES, heroes_only={KAS, spurt, kalix, hank})
    adventure = catalogue.adventure(7)

    fast = recommend(catalogue, account, adventure, "Push", models=models)
    proven = recommend(catalogue, account, adventure, "Push", models=models, solver="exact", exact_time_s=20)

    assert fast.main_dps.champion.id == KAS
    generic = _domain(recorded, heroes_only={KAS, spurt, kalix, hank})[2]
    seats = [Seat(s.champion.id, s.slot) for s in fast.seats if s.champion]
    main = Seat(KAS, fast.main_dps.slot)
    plain = generic.scorer(adventure.layout, "Push", main).score(seats).contributions
    found = fast.seats and {s.champion.id: s.contribution for s in fast.seats if s.champion}
    for evil in (spurt, kalix):  # Evil: their own buffs, then +25% to Kas on top
        assert 1 + found[evil].damage_pct / 100 == pytest.approx((1 + plain[evil].damage_pct / 100) * 1.25)
    assert found[hank].damage_pct == pytest.approx(plain[hank].damage_pct)
    # The gap is an honest upper bound: the proven best is within it.
    assert proven.score <= fast.score * (1 + fast.gap_pct / 100) * (1 + 1e-9)
    assert fast.gap_pct == pytest.approx(0, abs=1e-6)  # per-champion stacks are separable: bounds exact


# --- Briv ------------------------------------------------------------------------------------------

HASTE_UPGRADE = 95801


def _briv_tables():
    return {
        "upgrade_defines": [
            {"id": HASTE_UPGRADE, "hero_id": BRIV, "required_level": 80, "effect": "effect_def,95801",
             "name": "Unnatural Haste"},
            {"id": 95802, "hero_id": BRIV, "required_level": 80, "effect": "effect_def,95802",
             "specialization_name": "Metalborn", "specialization_description": "Haste stacks last longer."},
        ],
        "effect_defines": [
            {"id": 95801, "effect_keys": [{"effect_string": "briv_unnatural_haste,25,48,4,100,1"}]},
            {"id": 95802, "effect_keys": [{"effect_string": "briv_unnatural_haste_stack_consume_percent_override,3.2"}]},
        ],
        "loot_defines": [{"id": 95803, "hero_id": BRIV, "slot_id": 4, "rarity": 4, "name": "Boots of Haste",
                          "effects": [{"effect_string": f"buff_upgrade,100,{HASTE_UPGRADE}"}]}],
    }


def _briv(recorded, item_level: int | None = 1501, **kw):
    gear = [(BRIV, 4, 4, item_level)] if item_level else []
    return _domain(recorded, tables=_briv_tables(), owned=(BRIV,), gear=gear, **kw)


def test_briv_skip_value_halves_above_100_percent_adding_an_area(recorded):
    """25% base skip value, raised by the Epic boots' 100% × (1 + (item level − 1) / 250); while over
    100% it halves and Briv skips one more area."""
    expected = {1: (50, 1), 501: (100, 1), 1001: (75, 2), 1501: (100, 2), 3501: (100, 3)}
    for item_level, (chance, areas) in expected.items():
        explained = _briv(recorded, item_level)[2].explain(BRIV, Loadout())
        assert (explained.value("Skip chance"), explained.value("Areas skipped")) == (chance, areas), item_level
    assert _briv(recorded, None)[2].explain(BRIV, Loadout()).value("Skip chance") == 25  # no boots


@pytest.mark.parametrize("case", TOOLTIPS["cases"], ids=lambda c: c["name"])
def test_tooltip_fixture(recorded, case):
    tables = _briv_tables() if case["definitions"] == "briv" else case["definitions"]
    gear = [(case["champion_id"], slot, rarity, level) for slot, rarity, level in case["gear"]]
    _, _, models = _domain(recorded, tables=tables, owned=(case["champion_id"],) if case["champion_id"] != KAS else (), gear=gear)

    explained = models.explain(case["champion_id"], Loadout(tuple(case["specializations"])))

    for label, value in case["expect"].items():
        assert explained.value(label) == pytest.approx(value), label


def test_briv_metalborn_spends_fewer_stacks_per_jump(recorded):
    """4% of the stacks per jump from 48 (EM): 71 stacks last 10 jumps; Metalborn's 3.2%: 68."""
    _, _, models = _briv(recorded)

    assert models.explain(BRIV, Loadout()).value("Haste stacks for 10 jumps") == 71
    metalborn = models.explain(BRIV, Loadout((95802,)))
    assert metalborn.value("Haste stacks used per jump") == 3.2
    assert metalborn.value("Haste stacks for 10 jumps") == 68


def test_briv_monte_carlo_matches_the_expected_walk(recorded):
    # 1001: 75% to skip 2 areas: each area played moves 1 + 0.75 × 2 = 2.5 areas on average.
    _, _, models = _briv(recorded, 1001)

    played = models.explain(BRIV, Loadout()).value("Areas played to area 100")

    assert played == pytest.approx(99 / 2.5 + 1, rel=0.03)


def test_briv_skips_raise_gems_per_hour_and_state_the_monte_carlo(recorded):
    """A seated Briv with skips walks fewer areas, so the run is faster: more gems per hour."""
    catalogue, account, models = _briv(recorded)
    _, without_account, without_models = _domain(recorded, tables=_briv_tables())
    adventure = catalogue.adventure(7)

    rec = recommend(catalogue, account, adventure, "Gem Farm", models=models)
    plain = recommend(catalogue, without_account, adventure, "Gem Farm", models=without_models)

    assert BRIV in {s.champion.id for s in rec.seats if s.champion}
    assert rec.score > 1.5 * plain.score
    assert any("Briv" in a and "Monte-Carlo" in a for a in rec.scoring_assumptions)
    assert not any("Briv:" in a for a in plain.scoring_assumptions)


def test_a_next_step_names_the_scoring_assumption_its_gain_depends_on(recorded, tmp_path):
    """Story 73: a Briv gear step's Gem Farm gain rests on Briv's simulated skips, so its reason
    names that assumption rather than the Run Goal's general one."""
    raw = _changed(recorded, tables=_briv_tables(), owned=(BRIV,), gear=[(BRIV, 4, 4, 1001)],
                   heroes_only={BRIV, ARKHAN, CELESTE})
    log = tmp_path / "requestlog.log.gz"
    write_responses(raw, log)
    client = TestClient(create_app(log))
    client.post("/adventures/7/focus", params={"goal": "Gem Farm", "pin": "true"})
    client.post("/adventures/2036/focus", params={"goal": "Gem Farm", "pin": "false"})

    page = client.get("/next-steps/steps", params={"goal": "Gem Farm", "all": "true"}).text

    reason = next(r["text"] for r in rows(page, "step-reason") if r["text"].startswith("Blacksmith contract on Briv's slot 4"))
    assert reason.split("Scoring Assumption: ")[1].startswith("Briv: ")


def test_briv_gem_farm_gap_is_an_upper_bound(recorded):
    catalogue, account, models = _briv(recorded, heroes_only={BRIV, ARKHAN, BRUENOR, CELESTE, 4, 5})
    adventure = catalogue.adventure(7)

    fast = recommend(catalogue, account, adventure, "Gem Farm", models=models)
    oracle = recommend(catalogue, account, adventure, "Gem Farm", models=models, solver="exhaustive")

    assert oracle.score <= fast.score * (1 + fast.gap_pct / 100) * (1 + 1e-9)
    assert fast.score <= oracle.score * (1 + 1e-9)


# --- Breakers --------------------------------------------------------------------------------------


def _armoured_pure_energy(raw, chunks=25):
    for define in raw["getuserdetails"]["defines"]["adventure_defines"]:
        if define["id"] == 9:
            define["game_changes"] = [
                *(define.get("game_changes") or ()),
                {"type": "set_monster_health_type", "health_type": "armor_based", "amount": chunks, "target_class": "boss"},
            ]


def _ability(hero_id, effect_id, *keys):
    return (
        {"id": effect_id, "hero_id": hero_id, "required_level": 10, "effect": f"effect_def,{effect_id}", "name": "Ability"},
        {"id": effect_id, "effect_keys": list(keys)},
    )


def _tables(*abilities):
    return {"upgrade_defines": [a[0] for a in abilities], "effect_defines": [a[1] for a in abilities]}


@pytest.fixture(scope="module")
def armoured(recorded):
    raw = copy.deepcopy(recorded)
    _armoured_pure_energy(raw)
    return raw


def test_lucius_acid_breaks_armoured_bosses_faster(armoured):
    """Acid Splash: +1 segment per hit of his; armour-eating acid doubles the armour every hit removes.
    Pure Energy's bosses armoured with 25 chunks: one champion hitting every 5 s needs 125 s alone."""
    lucius = _tables(_ability(LUCIUS, 97201, {"effect_string": "lucius_armor_eating_acid,1"},
                              {"effect_string": "unique_hit_multiplier_from_attacks,100"}))
    catalogue, account, models = _domain(armoured, tables=lucius, owned=(LUCIUS,))
    _, without_account, without_models = _domain(armoured, owned=(LUCIUS,))
    adventure = catalogue.adventure(9)

    rec = recommend(catalogue, account, adventure, "Push", models=models)
    plain = recommend(catalogue, without_account, adventure, "Push", models=without_models)

    assert plain.push_verdict.area == 4  # generic layer: Lucius adds nothing to breaking
    assert LUCIUS in {s.champion.id for s in rec.seats if s.champion}
    assert not any(a.startswith("Breaking:") and "area 5 " in a for a in rec.push_verdict.assumptions)
    assert rec.push_verdict.area > 4
    explained = models.explain(LUCIUS)
    assert explained.value("Extra segments per hit") == 1
    assert explained.value("Armour loss multiplier") == 2


def _kleeb():
    return _tables(_ability(HEW_MAAN, 97501, {"effect_string": "unique_hit_multiplier,100",
                                              "filter_targets": ["boss"], "targets": ["all"]}))


def test_hew_maan_doubles_segments_only_from_a_middle_column(recorded):
    """Kleeb on top (middle columns): bosses lose twice the segments per hit. Free Play's
    Resurrected Hero of the Vale (50 hits, area 20) with Arkhan and Hew Maan hitting every 5 s:
    0.4 hits/s × 120 s = 48 < 50 bare, 96 from a middle column."""
    catalogue, account, models = _domain(recorded, tables=_kleeb(), owned=(HEW_MAAN,), heroes_only={ARKHAN, HEW_MAAN})
    adventure = catalogue.adventure(2040)
    columns = sorted({s.column for s in adventure.layout})
    middle = next(s.index for s in adventure.layout if columns[0] < s.column < columns[-1])
    edge = next(s.index for s in adventure.layout if s.column == columns[0])
    arkhan_slot = next(s.index for s in adventure.layout if s.index not in (middle, edge))
    main = Seat(ARKHAN, arkhan_slot)
    scorer = models.scorer(adventure.layout, "Push", main)

    assert scorer.breaking_rate((main, Seat(HEW_MAAN, middle)), "hits") * 120 == pytest.approx(96)
    assert scorer.breaking_rate((main, Seat(HEW_MAAN, edge)), "hits") * 120 == pytest.approx(48)
    assert models.explain(HEW_MAAN).value("Boss hit multiplier") == 2

    rec = recommend(catalogue, account, adventure, "Push", models=models)
    hew = next(s for s in rec.seats if s.champion and s.champion.id == HEW_MAAN)
    assert columns[0] < {s.index: s for s in adventure.layout}[hew.slot].column < columns[-1]
    assert not any(a.startswith("Breaking:") for a in rec.push_verdict.assumptions)


def test_gazrick_ablation_lowers_the_armour_threshold(recorded):
    """Armor Ablation 50% × 10 stacks: chunks need 1 − 0.5¹⁰ = 99.90234375% less damage. Bosses in one
    armour chunk, Main DPS hitting twice a second: its single hits stop reaching the chunk's health
    areas before its damage wall (60 hits in 30 s), unless Gazrick lowers the threshold."""
    raw = copy.deepcopy(recorded)
    _armoured_pure_energy(raw, chunks=1)
    raw["getdefinitions"]["attack_defines"] = [{"id": 25, "cooldown": 0.5}]  # Arkhan hits twice a second
    gazrick = _tables(_ability(GAZRICK, 99801, {"effect_string": "gazrick_armor_ablation,50", "max_stacks": 10}))
    catalogue, account, models = _domain(raw, tables=gazrick, owned=(GAZRICK,))
    adventure = catalogue.adventure(9)
    main = Seat(ARKHAN, 0)
    other = next(s.index for s in adventure.layout if s.index != 0)

    alone = push_verdict(catalogue, account, models, adventure, None, (main,), main)
    helped = push_verdict(catalogue, account, models, adventure, None, (main, Seat(GAZRICK, other)), main)

    assert models.explain(GAZRICK).value("Armour threshold reduction") == pytest.approx(99.90234375)
    assert any("no hit reaches its chunk health" in a for a in alone.assumptions)
    assert helped.area > alone.area


# --- Model Status ----------------------------------------------------------------------------------


def test_champion_page_explains_a_reconstruction(client):
    page = client.get(f"/champions/{KAS}").text

    [status] = rows(page, "model-status")
    assert status["text"] == "Model Status: Stale"
    assert rows(page, "model-evidence")[0]["text"].startswith("Description tests: 1 (")
    assert [r["text"] for r in rows(page, "explanation")] == ["Born Into Evil: no definition loaded."]


def test_a_reconstructed_formation_passing_the_live_check_is_verified(live):
    from fake_game import user_details

    def kas_alone(bud: float) -> dict:
        body = user_details()
        for instance in body["details"]["game_instances"]:
            if int(instance["game_instance_id"]) == 2:
                instance["formation"] = [KAS] + [-1] * 8
                instance["stats"]["this_reset_highest_damage_dealt"] = repr(bud)
                instance["stats"]["this_reset_highest_damage_dealt_hero_id"] = str(KAS)
        return body

    def refresh(body):
        live.server.queue("getuserdetails", body)
        live.clock.advance(61)
        client.post("/refresh")

    client = live.start()
    refresh(kas_alone(1.0))
    check = next(r["text"] for r in rows(client.get("/model-status").text, "damage-check") if r["text"].startswith("Instance 2 "))
    predicted = float(check.split("model ")[1].split(" vs")[0])
    assert math.isfinite(predicted)

    refresh(kas_alone(predicted * 1.01))

    assert rows(client.get(f"/champions/{KAS}").text, "model-status")[0]["text"] == "Model Status: Verified"
    assert champion_models.RECONSTRUCTIONS[KAS].description_tests


# --- one-change hints keep their pruning honest with reconstructions --------------------------------


def _oracle_hints(catalogue, account, adventure, run_goal, keep):
    """(gains `measure` reports, gains a full re-search of every change finds)."""
    rec = recommend(catalogue, account, adventure, run_goal, solver="exhaustive")
    changes = tuple(c for c in candidate_changes(catalogue, account, potions=False) if keep(c))
    found = {h.change.action: h.gain_pct for h in measure(catalogue, account, rec, changes, solver="exhaustive")}
    positive = {}
    for change in changes:
        changed_account, models = what_if(catalogue, account, change)
        after = recommend(catalogue, changed_account, adventure, run_goal, models=models, solver="exhaustive")
        if after.score and (gain := (after.score / rec.score - 1) * 100) > 0.01:
            positive[change.action] = gain
    return found, positive


def test_briv_hints_on_gem_farm_are_measured_not_pruned(recorded):
    catalogue, account, _ = _briv(recorded, 1001, heroes_only={BRIV, ARKHAN, CELESTE})
    adventure = replace(catalogue.adventure(7), layout=DIAMOND, blocked_slots=frozenset())

    found, positive = _oracle_hints(
        catalogue, account, adventure, "Gem Farm",
        lambda c: c.kind == "gear" and c.champion_id in (BRIV, ARKHAN) and c.gear.slot in (1, 4),
    )

    assert any("Briv's slot 4" in action for action in positive)  # more skip chance: faster runs
    assert set(found) == set(positive)
    for action, gain in positive.items():
        assert found[action] == pytest.approx(gain, rel=1e-6)


def test_breaker_hints_on_push_are_measured_not_pruned(armoured):
    lucius = _tables(_ability(LUCIUS, 97201, {"effect_string": "lucius_armor_eating_acid,1"},
                              {"effect_string": "unique_hit_multiplier_from_attacks,100"}))
    catalogue, account, _ = _domain(armoured, tables=lucius, heroes_only={ARKHAN, BRUENOR, CELESTE})
    adventure = replace(catalogue.adventure(9), layout=DIAMOND, blocked_slots=frozenset())

    found, positive = _oracle_hints(catalogue, account, adventure, "Push",
                                    lambda c: c.kind == "champion" and c.champion_id in (LUCIUS, HEW_MAAN))

    assert any("Lucius" in action for action in positive)
    assert set(found) == set(positive)
