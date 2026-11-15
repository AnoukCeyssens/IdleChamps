""""What would make this better" hints (#34): the rail through the HTTP app; the one-change
engine's pruning against an exhaustive re-search (the search oracle) on Seam 2.

The recording has no `buff_defines` (potions), no `hero_feat_defines` and no gear definitions for
Celeste, so HINT_TABLES adds small hand-written ones on top of EFFECT_TABLES. Shapes: feats and
loot as in research/game-api-and-data-shape.md; `buff_defines` rows (id, name, description,
effect) are a guess, unverified against a real response.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from advisor.account import parse_account
from advisor.catalogue import parse_catalogue
from advisor.hints import candidate_changes, hints, measure, what_if
from advisor.recommendation import recommend, with_search_problem
from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from advisor.results_codec import Decoder, Encoder
from conftest import FIXTURE_LOG, rows
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
def mad_wizard_hints(raw, tmp_path_factory) -> dict[str, str]:
    """The Mad Wizard's Gold Farm rail, through the HTTP app: action -> shown gain."""
    responses = dict(raw)
    responses["getdefinitions"] = {**raw["getdefinitions"], **HINT_TABLES}
    log = tmp_path_factory.mktemp("hints") / "requestlog.log.gz"
    write_responses(responses, log)
    page = TestClient(create_app(log)).get("/adventures/7/hints", params={"goal": "Gold Farm"}).text
    shown = [r["text"].removesuffix(" Torm's Favor per hour").rsplit(" → ", 1) for r in rows(page, "hint")]
    return dict(shown)


def _gains(found):
    return {h.change.action: h.gain_pct for h in found}


def _number(gain: str) -> float:
    return float(gain.strip("+%"))


def test_a_potion_is_a_hint_with_its_gain(mad_wizard_hints):
    # +100% damage is worth 2^0.332 = +25.9% Favor per hour.
    assert mad_wizard_hints["Use a Potion of the Giant's Strength"] == "+26%"


def test_the_baseline_never_includes_potions(hinted, raw):
    catalogue, account = hinted
    no_potions = {k: v for k, v in HINT_TABLES.items() if k != "buff_defines"}
    plain = parse_catalogue({**raw, "getdefinitions": {**raw["getdefinitions"], **no_potions}})

    with_potions = recommend(catalogue, account, catalogue.adventure(7), "Gold Farm")
    without = recommend(plain, account, plain.adventure(7), "Gold Farm")

    assert with_potions.score == pytest.approx(without.score)
    assert "No potions." in with_potions.scoring_assumptions


def test_each_kind_of_change_is_measured_by_a_re_search(mad_wizard_hints):
    # Evelyn's +300% to all replaces a seat-6 champion who adds nothing: 4^0.332 = +58.4%.
    assert mad_wizard_hints["Get Evelyn (The Great Modron March, Year 1)"] == "+58%"
    # Feat: +50% to all, 1.5^0.332 = +14.4%.
    assert mad_wizard_hints["Unlock Celeste's feat Inspiring Presence"] == "+14%"
    # Chest: Blessing 100% × (1 + 100% + 50%·1.016) → × (1 + 100% + 100%·1.016): (4.016/3.508)^0.332 = +4.59%.
    assert mad_wizard_hints["Open a chest for Celeste's slot 2 (Rare → Epic)"] == "+4.6%"
    # Contract: item level 5 → 29 (1 Large contract): 50% × 1.016 → 50% × 1.112 = +0.45%.
    assert mad_wizard_hints["Blacksmith contract on Celeste's slot 2 (item level 5 → 29)"] in ("+0.4%", "+0.5%")


def test_zero_gain_changes_are_not_shown_and_the_rest_rank_best_first(mad_wizard_hints):
    assert "Use a Potion of Speed" not in mad_wizard_hints  # game speed isn't modelled: no gain
    assert not any("Bruenor" in action for action in mad_wizard_hints)  # no modelled gear effects
    gains = [_number(g) for g in mad_wizard_hints.values()]
    assert gains == sorted(gains, reverse=True) and len(gains) <= 10  # the rail's best ten


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


@pytest.mark.parametrize("goal", ["Gem Farm", "Gold Farm", "Push"])
def test_a_persisted_result_drops_its_search_problem_and_hints_rebuild_it(hinted, goal):
    catalogue, account = hinted
    rec = recommend(catalogue, account, catalogue.adventure(7), goal)
    assert pickle.loads(pickle.dumps(rec)).problem == rec.problem  # crosses the recompute pool intact

    encoded = Encoder(catalogue)(rec)
    assert "problem" not in encoded["fields"]  # derived search state is not persisted
    restored = Decoder(catalogue)(json.loads(json.dumps(encoded)))
    assert restored == rec and restored.problem is None

    rebuilt = with_search_problem(catalogue, account, restored)
    assert rebuilt.problem == rec.problem
    assert hints(catalogue, account, rebuilt) == hints(catalogue, account, rec)
