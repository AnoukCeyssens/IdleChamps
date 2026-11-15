"""Push tab and the damage-only Push Verdict (#32), through the HTTP app.

Recording facts used: instance 1 runs The Crypt of Legends (Morte, adventure 2036, event curve) with
Kas (153) setting a BUD of 1.53e124; instance 2 runs Pure Energy (9, core curve) with Asharra (6) at
BUD 4.27e27. Best areas: Mad Wizard (7) 50 of objective 50, Pure Energy 84 of 100.
"""

from __future__ import annotations

import math
import re

import pytest
from fastapi.testclient import TestClient

from advisor.catalogue import CORE_HEALTH_CURVE, HealthCurve
from advisor.push_verdict import wall_area
from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows

PUSH = "/adventures/{}?goal=Push"


def _app(tmp_path_factory, name, change) -> TestClient:
    responses = read_responses(FIXTURE_LOG)
    change(responses)
    log = tmp_path_factory.mktemp(name) / "requestlog.log.gz"
    write_responses(responses, log)
    return TestClient(create_app(log))


def _one(page: str, kind: str) -> str:
    (row,) = rows(page, kind)
    return row["text"]


def test_push_tab_shows_a_recommendation_and_a_push_verdict(client):
    page = client.get(PUSH.format(9)).text

    assert _one(page, "main-dps").startswith("Main DPS: ")
    assert rows(page, "seat-row")
    assert rows(page, "push-verdict")


def test_reached_before_wins_when_my_best_area_meets_the_objective(client):
    page = client.get(PUSH.format(7)).text

    assert _one(page, "push-verdict") == "Reached before (area 50)"
    assert _one(page, "best-here") == "best here: 50"
    assert not rows(page, "push-estimate")


def _assumptions(page: str) -> list[str]:
    return [r["text"] for r in rows(page, "assumption")]


def test_push_states_random_effect_click_and_daily_cap_assumptions(client):
    assumptions = " ".join(_assumptions(client.get(PUSH.format(9)).text)).lower()

    assert "random effects are taken at their expected value" in assumptions
    assert "active player" in assumptions and "daily cap" in assumptions


def test_estimate_is_scaled_from_the_anchor_run_named_in_the_scoring_assumptions(client):
    page = client.get(PUSH.format(9) + "&patron=5").text  # Elminster, objective 825: Kas is the Main DPS

    assert _one(page, "main-dps").startswith("Main DPS: Kas")
    anchor = [a for a in _assumptions(page) if a.startswith("Anchor Run:")]
    assert anchor == ["Anchor Run: instance 1 on The Crypt of Legends (Morte) (area 419), Main DPS Kas, BUD 1.53e124."]


def test_a_different_main_dps_than_the_anchor_widens_the_band_by_5(client):
    page = client.get(PUSH.format(9)).text  # no patron: the Main DPS is neither Kas nor Asharra

    main = _one(page, "main-dps")
    assert not main.startswith("Main DPS: Kas") and not main.startswith("Main DPS: Asharra")
    (anchor,) = [a for a in _assumptions(page) if a.startswith("Anchor Run:")]
    assert "Main DPS Kas, BUD 1.53e124" in anchor  # the highest BUD
    assert "the band is 5 wider" in anchor
    (band,) = [a for a in _assumptions(page) if a.startswith("Band ±")]
    assert "10 on core campaigns, +5 for the anchor's different Main DPS" in band


def test_estimate_is_rounded_to_5_and_shows_its_band_and_objective(client):
    page = client.get(PUSH.format(9)).text

    estimate = _one(page, "push-estimate")
    match = re.fullmatch(r"Expected to reach ~(\d+) \(±(\d+)\) · objective 100", estimate)
    assert match, estimate
    assert int(match.group(1)) % 5 == 0
    (band,) = [a for a in _assumptions(page) if a.startswith("Band ±")]
    assert band.startswith(f"Band ±{match.group(2)} areas")
    assert _one(page, "best-here") == "best here: 84"


def test_likely_is_capped_at_borderline_while_monster_placement_is_unknown(client):
    # Towering Expectations (objective 250): its Area Set 68 isn't in the recording.
    page = client.get(PUSH.format(413)).text

    assert _one(page, "push-verdict") == "Borderline"
    assert rows(page, "push-cap")
    assert (
        "Armoured or hits-based bosses not checked (positions unknown): the verdict is at best Borderline."
        in _assumptions(page)
    )


def test_unlikely_when_the_estimate_falls_short_by_more_than_the_band(client):
    page = client.get(PUSH.format(9) + "&patron=5").text  # objective 825

    assert _one(page, "push-verdict") == "Unlikely"
    assert _one(page, "push-estimate").endswith("· objective 825")


def test_free_play_shows_the_estimate_without_a_verdict(client):
    page = client.get(PUSH.format(2040)).text

    assert _one(page, "push-verdict") == "No objective (free play)"
    assert _one(page, "push-estimate").startswith("Expected to reach ~")
    assert _one(page, "best-here") == "best here: none"


@pytest.fixture(scope="module")
def veteran(tmp_path_factory) -> TestClient:
    """Pure Energy's best area raised to 900: above its own objective (100) and every patron's."""

    def raise_best(responses):
        responses["getuserdetails"]["details"]["stats"]["highest_area_completed_ever_c9"] = 900

    return _app(tmp_path_factory, "veteran", raise_best)


def test_patron_objectives_are_always_estimated(veteran):
    plain = veteran.get(PUSH.format(9)).text
    patron = veteran.get(PUSH.format(9) + "&patron=1").text  # Mirt, objective 250

    assert _one(plain, "push-verdict") == "Reached before (area 900)"
    assert _one(patron, "push-verdict") in ("Likely", "Borderline", "Unlikely")
    assert _one(patron, "push-estimate").endswith("· objective 250")
    assert _one(patron, "best-here") == "best here: 900"
    assert any(a.startswith("Patron objectives are always estimated") for a in _assumptions(patron))


@pytest.fixture(scope="module")
def no_bud(tmp_path_factory) -> TestClient:
    def drop_buds(responses):
        for instance in responses["getuserdetails"]["details"]["game_instances"]:
            instance["stats"].pop("this_reset_highest_damage_dealt", None)

    return _app(tmp_path_factory, "no_bud", drop_buds)


def test_no_estimate_without_a_running_instance_with_a_bud(no_bud):
    page = no_bud.get(PUSH.format(9)).text

    assert _one(page, "push-verdict") == "No estimate: start any Push run and Refresh"
    assert not rows(page, "push-estimate")
    assert _one(page, "best-here") == "best here: 84"
    assert rows(page, "seat-row")  # the recommendation is still shown


def test_escort_adventures_are_not_estimated_but_still_recommended(client):
    page = client.get(PUSH.format(2038)).text  # Cacophony of Curses: Morte's Friend sits in slot 4

    assert _one(page, "push-verdict") == "Estimate not available: escort adventure"
    assert not rows(page, "push-estimate")
    assert _one(page, "main-dps").startswith("Main DPS: ")


def test_unhandled_modifiers_are_not_estimated(client):
    page = client.get(PUSH.format(1456)).text  # The Circle of Stars: global_effects

    assert _one(page, "push-verdict") == "Estimate not available: unhandled adventure modifier (global_effects)"


@pytest.fixture(scope="module")
def tougher(tmp_path_factory) -> TestClient:
    """Towering Expectations (413, core, objective 250) with monsters at ×1000 health."""

    def toughen(responses):
        for define in responses["getuserdetails"]["defines"]["adventure_defines"]:
            if define["id"] == 413:
                define["game_changes"] = [
                    *(define.get("game_changes") or ()),
                    {"type": "add_monster_properties", "properties": {"health_multiplier": 1000}},
                ]

    return _app(tmp_path_factory, "tougher", toughen)


# --- the damage wall: a pure function, checked on worked numbers ----------------------------------


def test_the_damage_wall_is_the_first_boss_the_damage_cannot_kill():
    # Core boss health, log10: 1 (base 10) + 1.699 (×50) + (area − 1) × log10(2.031) = 0.30771.
    # Area 95: 31.623, area 100: 33.162, area 105: 34.701.
    assert wall_area(CORE_HEALTH_CURVE, math.log(1e33)) == 99
    assert wall_area(CORE_HEALTH_CURVE, math.log(10**33.2)) == 104
    event = HealthCurve(10.0, ((1, 1.85),))  # area 100 on the event curve: 2.699 + 99 × 0.26717 = 29.149
    assert wall_area(event, math.log(10**29.1)) == 99


def test_only_stale_or_check_failed_champions_widen_the_band_and_get_the_badge(client):
    """Unverified means Check Failed or Stale (story 93: their full model is used, unverified). Not
    Modelled champions are scored on generic effects only (story 92) and don't count. In the
    recording only Kas is reconstructed (never checked live: Stale); he is seated on Pure Energy and
    is the Anchor Run's Main DPS."""
    page = client.get(PUSH.format(9)).text
    (band,) = [a for a in _assumptions(page) if a.startswith("Band ±")]
    assert band == (
        "Band ±18 areas: 10 on core campaigns, +5 for the anchor's different Main DPS, "
        "+3 for each of 1 unverified champion."
    )
    assert _one(page, "unverified-badge") == "1 champion unverified, score may be off"

    event = client.get(PUSH.format(2037)).text  # an event Crypt: Kas only in the anchor's formation
    (band,) = [a for a in _assumptions(event) if a.startswith("Band ±")]
    assert band.startswith("Band ±20 areas: 12 on event campaigns, +5 for the anchor's different Main DPS, +3 for each of 1")
    assert not rows(event, "unverified-badge")


def _expected(page: str) -> int:
    return int(re.match(r"Expected to reach ~(\d+)", _one(page, "push-estimate")).group(1))


def test_health_multiplier_modifiers_lower_the_estimate(client, tougher):
    plain = client.get(PUSH.format(413)).text
    tough = tougher.get(PUSH.format(413)).text

    # ×1000 health costs log(1000)/log(2.031) ≈ 9.75 areas: two boss areas.
    assert _expected(plain) - _expected(tough) == 10
    assert any("×1000 from the adventure's monster modifiers" in a for a in _assumptions(tough))
