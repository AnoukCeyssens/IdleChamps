""""What would make this better" hints (#34), Seam 2: the one-change engine on a recommendation.

The recording has no `buff_defines` (potions), no `hero_feat_defines` and no gear definitions for
Celeste, so HINT_TABLES adds small hand-written ones on top of EFFECT_TABLES. Shapes: feats and
loot as in research/game-api-and-data-shape.md; `buff_defines` rows (id, name, description,
effect) are a guess, unverified against a real response.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from advisor.account import parse_account
from advisor.catalogue import parse_catalogue
from advisor.hints import candidate_changes, hints, measure, what_if
from advisor.recommendation import recommend
from advisor.request_log import read_responses
from conftest import FIXTURE_LOG
from test_recommendation import DIAMOND, EFFECT_TABLES

HINT_TABLES = {
    **EFFECT_TABLES,
    "upgrade_defines": [
        *EFFECT_TABLES["upgrade_defines"],
        # Evelyn (not owned): a big buff to every champion.
        {"id": 90007, "hero_id": 26, "required_level": 10, "effect": "effect_def,90007", "name": "Battle Hymn"},
    ],
    "effect_defines": [
        *EFFECT_TABLES["effect_defines"],
        {"id": 90007, "effect_keys": [{"effect_string": "global_dps_multiplier_mult,300", "targets": ["all"]}]},
    ],
    # Celeste's slot 2 (she holds a Rare at item level 5) raises her Blessing.
    "loot_defines_90": [
        {"id": 990023, "hero_id": 2, "slot_id": 2, "rarity": 3, "name": "Holy Symbol",
         "effects": [{"effect_string": "buff_upgrade,50,90001"}]},
        {"id": 990024, "hero_id": 2, "slot_id": 2, "rarity": 4, "name": "Radiant Holy Symbol",
         "effects": [{"effect_string": "buff_upgrade,100,90001"}]},
    ],
    # A feat Celeste doesn't own yet (she has 1 feat slot).
    "hero_feat_defines": [
        {"id": 99001, "hero_id": 2, "name": "Inspiring Presence", "description": "+50% damage to all.",
         "effects": [{"effect_string": "global_dps_multiplier_mult,50"}]},
    ],
    "buff_defines": [
        {"id": 71, "name": "Potion of the Giant's Strength", "description": "+100% damage for all Champions.",
         "effect": "global_dps_multiplier_mult,100"},
        {"id": 72, "name": "Potion of Speed", "description": "Game speed +25%.", "effect": "time_scale_mult,25"},
    ],
}


@pytest.fixture(scope="module")
def raw():
    return read_responses(FIXTURE_LOG)


@pytest.fixture(scope="module")
def hinted(raw):
    responses = dict(raw)
    responses["getdefinitions"] = {**raw["getdefinitions"], **HINT_TABLES}
    return parse_catalogue(responses), parse_account(responses)


@pytest.fixture(scope="module")
def mad_wizard_hints(hinted):
    catalogue, account = hinted
    rec = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")
    return rec, measure(catalogue, account, rec, candidate_changes(catalogue, account))


def _gains(found):
    return {h.change.action: h.gain_pct for h in found}


def test_a_potion_is_a_hint_with_its_gain(mad_wizard_hints):
    _, found = mad_wizard_hints

    # +100% damage is worth 2^0.332 = +25.9% Favor per hour.
    assert _gains(found)["Use a Potion of the Giant's Strength"] == pytest.approx(25.88, abs=0.01)


def test_the_baseline_never_includes_potions(hinted, raw):
    catalogue, account = hinted
    no_potions = {k: v for k, v in HINT_TABLES.items() if k != "buff_defines"}
    plain = parse_catalogue({**raw, "getdefinitions": {**raw["getdefinitions"], **no_potions}})

    with_potions = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")
    without = recommend(plain, account, plain.adventure(7), "Gold Farm")

    assert with_potions.score == pytest.approx(without.score)
    assert "No potions." in with_potions.scoring_assumptions


def test_each_kind_of_change_is_measured_by_a_re_search(mad_wizard_hints):
    _, found = mad_wizard_hints
    gains = _gains(found)

    # Evelyn's +300% to all replaces a seat-6 champion who adds nothing: 4^0.332 = +58.4%.
    assert gains["Get Evelyn (The Great Modron March, Year 1)"] == pytest.approx(58.45, abs=0.01)
    # Feat: +50% to all, 1.5^0.332 = +14.4%.
    assert gains["Unlock Celeste's feat Inspiring Presence"] == pytest.approx(14.40, abs=0.01)
    # Chest: Blessing 100% × (1 + 100% + 50%·1.016) → × (1 + 100% + 100%·1.016): (4.016/3.508)^0.332.
    assert gains["Open a chest for Celeste's slot 2 (Rare → Epic)"] == pytest.approx(4.59, abs=0.01)
    # Contract: item level 5 → 29 (1 Large contract): 50% × 1.016 → 50% × 1.112.
    assert gains["Blacksmith contract on Celeste's slot 2 (item level 5 → 29)"] == pytest.approx(0.45, abs=0.01)


def test_zero_gain_changes_are_not_shown(mad_wizard_hints):
    _, found = mad_wizard_hints

    assert all(h.gain_pct > 0.01 for h in found)
    assert "Use a Potion of Speed" not in _gains(found)  # game speed isn't modelled: no gain
    assert not any("Bruenor" in action for action in _gains(found))  # no modelled gear effects
    assert [h.gain_pct for h in found] == sorted((h.gain_pct for h in found), reverse=True)


def test_hints_for_the_right_rail_are_the_best_ten(hinted, mad_wizard_hints):
    catalogue, account = hinted
    rec, everything = mad_wizard_hints

    top = hints(catalogue, account, rec)

    assert [h.change.action for h in top] == [h.change.action for h in everything[:10]]


def test_an_infeasible_recommendation_has_no_hints(hinted):
    catalogue, account = hinted
    adventure = replace(catalogue.adventure(7), layout=())

    assert hints(catalogue, account, recommend(catalogue, account, adventure, "Gold Farm")) == ()


# --- pruning: the cheap upper bound never drops a change that would have had a gain ----------


def _small(account, ids):
    return replace(account, champions={i: c for i, c in account.champions.items() if i in ids})


@pytest.mark.parametrize("run_goal", ["Gold Farm", "Push"])
@pytest.mark.parametrize("roster", [{12, 2, 1, 4}, {12, 153, 2, 5, 43}, {2, 1, 4}])
def test_pruning_never_drops_a_change_with_a_positive_gain(hinted, roster, run_goal):
    catalogue, account = hinted
    account = _small(account, roster)
    adventure = replace(catalogue.adventure(7), layout=DIAMOND, blocked_slots=frozenset())
    rec = recommend(catalogue, account, adventure, run_goal, solver="exhaustive")
    changes = candidate_changes(catalogue, account)
    # Only the roster's own gear and feats, and only unowned champions in their seats, keep this small.
    changes = tuple(c for c in changes if c.kind != "champion" or catalogue.champion(c.champion_id).seat in (1, 2, 6, 12))

    found = _gains(measure(catalogue, account, rec, changes, solver="exhaustive"))

    positive = {}
    for change in changes:
        changed_account, models = what_if(catalogue, account, change)
        after = recommend(catalogue, changed_account, adventure, run_goal, models=models, solver="exhaustive")
        gain = (after.score / rec.score - 1) * 100
        if gain > 0.01:
            positive[change.action] = gain
    assert positive  # the instance has changes worth making
    assert set(found) == set(positive)
    for action, gain in positive.items():
        assert found[action] == pytest.approx(gain, rel=1e-6)
