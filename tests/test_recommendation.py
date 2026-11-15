"""Seam 2 (spec #22): `recommend(catalogue, account, adventure, Run Goal, patron)` on the recorded
fixture, for what is awkward to assert through HTML: restriction safety and the search oracle.

The recording has no `effect_defines`, so tests of generic ability effects add a small hand-written
table (shapes from research/formation-strength-model.md: `effect_keys[{effect_string, targets}]`,
upgrades linking abilities with `effect_def,<id>`).
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from advisor.account import parse_account
from advisor.catalogue import Slot, parse_catalogue
from advisor.recommendation import recommend
from advisor.request_log import read_responses
from conftest import FIXTURE_LOG

KAS, BRUENOR, CELESTE, SPURT, CALLIPE, JARLAXLE = 153, 1, 2, 43, 5, 4


@pytest.fixture(scope="module")
def raw():
    return read_responses(FIXTURE_LOG)


@pytest.fixture(scope="module")
def catalogue(raw):
    return parse_catalogue(raw)


@pytest.fixture(scope="module")
def account(raw):
    return parse_account(raw)


def _with_definitions(raw, **tables):
    responses = dict(raw)
    responses["getdefinitions"] = {**raw["getdefinitions"], **tables}
    return parse_catalogue(responses), parse_account(responses)


def _seated(rec):
    return [s for s in rec.seats if s.champion is not None]


def test_gold_farm_recommendation_seats_only_owned_champions_one_per_seat(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")

    seated = _seated(rec)
    assert rec.infeasible_reason is None
    assert len(seated) == 9  # The Mad Wizard's 9-slot Diamond, all filled
    assert all(account.owns(s.champion.id) for s in seated)
    assert len({s.champion.seat for s in seated}) == len(seated)
    assert len({s.slot for s in seated}) == len(seated)
    assert rec.main_dps.champion in [s.champion for s in seated]
    assert rec.run_goal == "Gold Farm" and rec.patron is None


@pytest.mark.parametrize("patron", [None, 1, 2, 3, 4, 5])
def test_restrictions_are_never_broken(catalogue, account, patron):
    # Who may play is read straight from the champions' tags and character sheets.
    checks = {
        361: lambda c: "dps" not in c.tags,  # DPS's Day Off
        2024: lambda c: "evil" in c.tags,  # only Evil
        360: lambda c: "lawful" in c.tags or "good" in c.tags,  # Lawful and/or Good (Turiel forced)
        1457: lambda c: c.ability_scores["dex"] >= 15 or c.ability_scores["wis"] >= 13,  # (Tatyana forced)
        1497: lambda c: "tanking" not in c.tags,  # (Minthara forced)
    }
    patron_checks = {
        None: lambda c: True,
        1: lambda c: "good" in c.tags or "evil" in c.tags,
        2: lambda c: c.ability_scores["con"] >= 14,
        3: lambda c: c.ability_scores["int"] >= 13,
        4: lambda c: c.ability_scores["str"] >= 10 and c.ability_scores["cha"] >= 13,
        5: lambda c: max(c.date_available, c.last_rework_date) >= "2023-11-16",  # snapshot: 2026-11-15
    }
    for adventure_id, allowed in checks.items():
        adventure = catalogue.adventure(adventure_id)
        rec = recommend(catalogue, account, adventure, "Gold Farm", patron)
        forced = {f.champion_id for f in adventure.forced}
        assert rec.infeasible_reason is None
        for seat in _seated(rec):
            if seat.champion.id in forced:
                continue
            assert account.owns(seat.champion.id)
            assert allowed(seat.champion), (adventure_id, seat.champion.name)
            assert patron_checks[patron](seat.champion), (adventure_id, patron, seat.champion.name)


def test_forced_champion_sits_in_its_forced_slot(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(360), "Gold Farm")  # Turiel in slot 1

    turiel = next(s for s in rec.seats if s.slot == 1)
    assert turiel.champion.name == "Turiel"
    assert turiel.provided_by_adventure  # not owned: the adventure lends him


def test_escort_blocked_slots_stay_empty_and_marked(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(289), "Gold Farm")  # escorts on 0 and 2

    assert [s.slot for s in rec.seats if s.blocked] == [0, 2]
    assert all(s.champion is None for s in rec.seats if s.blocked)
    assert len(_seated(rec)) == 8


def test_restricted_champions_are_left_out_with_the_reason(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(2024), "Gold Farm")  # only Evil

    reasons = {left.champion.name: left.reason for left in rec.left_out}
    assert reasons["Bruenor"] == "not allowed: only Evil champions"
    assert "Arkhan" in [s.champion.name for s in _seated(rec)]


def test_patron_rules_are_named_in_left_out(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(9), "Gold Farm", patron=2)  # Vajra: CON 14+

    reasons = {left.champion.name: left.reason for left in rec.left_out}
    assert reasons["Asharra"] == "not allowed by Vajra: only champions with CON of at least 14"
    assert rec.patron.name == "Vajra"


def test_no_legal_formation_names_the_missing_piece(raw):
    responses = dict(raw)
    details = dict(raw["getuserdetails"]["details"])
    details["heroes"] = [h for h in details["heroes"] if int(h["hero_id"]) in (12, 153)]  # Arkhan, Kas: both DPS
    responses["getuserdetails"] = {**raw["getuserdetails"], "details": details}
    catalogue, account = parse_catalogue(responses), parse_account(responses)

    rec = recommend(catalogue, account, catalogue.adventure(361), "Gold Farm")  # DPS's Day Off

    assert rec.main_dps is None
    assert rec.infeasible_reason == (
        "No legal formation: needs at least 1 champion that fits \u201cno DPS champions\u201d, and none of yours do."
    )


# Hand-written abilities for owned champions, in the published shapes (see module docstring).
EFFECT_TABLES = {
    "upgrade_defines": [
        {"id": 90001, "hero_id": 2, "required_level": 10, "effect": "effect_def,90001", "name": "Blessing"},
        {"id": 90005, "hero_id": 2, "required_level": 50, "effect": "buff_upgrade,100,90001", "name": "Big Blessing"},
        {"id": 90002, "hero_id": 1, "required_level": 50, "effect": "effect_def,90002",
         "specialization_name": "Vanguard", "specialization_description": "Buffs the champion ahead."},
        {"id": 90003, "hero_id": 1, "required_level": 50, "effect": "effect_def,90003",
         "specialization_name": "Rearguard", "specialization_description": "Buffs the champion behind."},
        {"id": 90004, "hero_id": 4, "required_level": 10, "effect": "effect_def,90004", "name": "Purse"},
        {"id": 90006, "hero_id": 5, "required_level": 10, "effect": "effect_def,90006", "name": "Song"},
    ],
    "effect_defines": [
        {"id": 90001, "effect_keys": [{"effect_string": "hero_dps_multiplier_mult,100", "targets": ["adj"]}]},
        {"id": 90002, "effect_keys": [{"effect_string": "hero_dps_multiplier_mult,50", "targets": ["ahead"]}]},
        {"id": 90003, "effect_keys": [{"effect_string": "hero_dps_multiplier_mult,50", "targets": ["behind"]}]},
        {"id": 90004, "effect_keys": [{"effect_string": "gold_multiplier_mult,200"}]},
        {"id": 90006, "effect_keys": [
            {"effect_string": "calliope_unique_song,500"},
            {"effect_string": "global_dps_multiplier_mult,1000", "amount_expr": "upgrade_amount(90006,0)*2"},
        ]},
    ],
}


@pytest.fixture(scope="module")
def modelled(raw):
    return _with_definitions(raw, **EFFECT_TABLES)


def _by_name(rec):
    return {s.champion.name: s for s in _seated(rec)}


def test_positional_buff_raised_by_buff_upgrade_is_labelled_by_its_gain_to_the_main_dps(modelled):
    catalogue, account = modelled
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")

    main = rec.main_dps
    assert main.champion.name == "Arkhan"
    celeste = _by_name(rec)["Celeste"]
    layout = {s.index: s for s in catalogue.adventure(7).layout}
    assert main.slot in layout[celeste.slot].adjacent
    # 100% to adjacent champions, raised by 100% by Big Blessing.
    assert celeste.contribution_label == "+200% to Arkhan"


def test_specialization_is_chosen_to_fit_the_seat(modelled):
    catalogue, account = modelled
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")

    bruenor = _by_name(rec)["Bruenor"]
    layout = {s.index: s for s in catalogue.adventure(7).layout}
    main, here = layout[rec.main_dps.slot], layout[bruenor.slot]
    assert bruenor.contribution_label == "+50% to Arkhan"
    assert main.index in here.adjacent
    (spec,) = bruenor.specializations
    if spec.name == "Vanguard":  # buffs the champion ahead: Arkhan is one column nearer the front
        assert main.column == here.column - 1
    else:
        assert spec.name == "Rearguard"
        assert main.column == here.column + 1


def test_gold_effects_count_on_gold_farm_and_unique_effects_do_not(modelled):
    catalogue, account = modelled
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")

    seats = _by_name(rec)
    assert seats["Jarlaxle"].contribution_label == "+0% to Arkhan \u00b7 +200% gold"
    # Calliope's unique key and her expression-based effect are not modelled (generic effects only),
    # so she adds nothing and higher-level champions fill the 9 slots before her.
    reasons = {left.champion.name: left.reason for left in rec.left_out}
    assert reasons["Calliope"] == "no free slot: every slot holds a pick at least as good"
    # Favor per hour is compared as gold multipliers \u00d7 Main DPS damage^0.332 (a stated assumption).
    assert rec.score == pytest.approx(3 * rec.main_dps.bud**0.332)


def test_gold_farm_states_its_scoring_assumptions_and_unverified_champions(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")

    text = " ".join(rec.scoring_assumptions)
    assert "click" in text.lower() and "daily" in text.lower()
    assert "relative" in text
    assert set(rec.unverified_champions) == {s.champion for s in _seated(rec)}
    assert all(s.model_status.value == "Not Modelled" for s in _seated(rec))


def test_between_equal_scores_the_higher_level_champion_takes_the_seat(catalogue, account):
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")

    # Seat 1: Hank (level 750), Bruenor (300), Ezmerelda and Anson (0) add nothing modelled here.
    assert "Hank" in _by_name(rec)
    reasons = {left.champion.name: left.reason for left in rec.left_out}
    assert reasons["Bruenor"] == "seat 1 taken by Hank (same score, higher level)"


# --- search oracle: the fast search against every legal formation, on small instances --------

DIAMOND = (  # a synthetic 4-slot layout: one front slot, two in the middle, one at the back
    Slot(0, 30, 10, 0, (1, 2)),
    Slot(1, 20, 0, 1, (0, 3)),
    Slot(2, 20, 20, 1, (0, 3)),
    Slot(3, 10, 10, 2, (1, 2)),
)
LINE = tuple(Slot(i, 50 - 10 * i, 0, i, tuple(j for j in (i - 1, i + 1) if 0 <= j < 5)) for i in range(5))


def _small_roster(account, ids):
    return replace(account, champions={i: c for i, c in account.champions.items() if i in ids})


@pytest.mark.parametrize("run_goal", ["Gold Farm", "Push"])
@pytest.mark.parametrize(
    "adventure_id, layout, roster",
    [
        (7, DIAMOND, {12, 2, 1, 4, 5, 43}),  # unrestricted, synthetic layout
        (7, LINE, {12, 153, 2, 1, 4, 3, 43}),  # two seat-3 champions, two DPS candidates
        (2024, DIAMOND, {12, 153, 43, 73, 128, 9}),  # only Evil, with Kitiara forced in
        (361, LINE, {2, 1, 5, 43, 12}),  # no DPS: the Main DPS has to be a non-DPS champion
        (360, DIAMOND, {2, 1, 3, 12, 4}),  # Turiel forced into slot 1, Lawful/Good only
    ],
)
def test_fast_search_matches_an_exhaustive_solve_within_its_gap(modelled, adventure_id, layout, roster, run_goal):
    catalogue, account = modelled
    account = _small_roster(account, roster)
    adventure = replace(catalogue.adventure(adventure_id), layout=layout, blocked_slots=frozenset())

    fast = recommend(catalogue, account, adventure, run_goal)
    exact = recommend(catalogue, account, adventure, run_goal, solver="exhaustive")

    assert exact.exact and not fast.exact
    assert fast.score <= exact.score * (1 + 1e-9)
    assert fast.score * (1 + fast.gap_pct / 100) >= exact.score * (1 - 1e-9)
