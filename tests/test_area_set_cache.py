"""The Area Set Cache in live mode (#19, #33): kept across Refreshes and restarts, overwritten when a
set arrives again, never expired, each set with the game version it was last seen under.

The recording's getuserdetails brings Area Sets 6 (The Mad Wizard, Pure Energy) and 12 (the Crypt of
Legends event adventures); its changelog says game version 1.242.1 (v659).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from advisor.area_sets import area_sets_from
from advisor.request_log import read_responses
from advisor.store import Store
from conftest import FIXTURE_LOG, rows
from fake_game import user_details

PURE_ENERGY = "/adventures/9?goal=Push"
MORTE_FREE_PLAY = "/adventures/2040?goal=Push"


def _without(*area_set_ids: int) -> dict:
    body = user_details()
    defines = body["defines"]
    defines["adventure_area_defines"] = [
        a for a in defines["adventure_area_defines"] if a["area_set_id"] not in area_set_ids
    ]
    return body


def _verdict(page: str) -> str:
    (row,) = rows(page, "push-verdict")
    return row["text"]


def _assumptions(page: str) -> list[str]:
    return [r["text"] for r in rows(page, "assumption")]


def _settle(live, adventure_ids=(9, 2040)) -> None:
    """Run the pending Recompute jobs (computed in a worker, which sees the Area Set Cache too) for
    the adventures these tests open; the rest stay pending."""
    if not live.pools:
        return
    pool = live.pools[-1]
    mine = [job for job in pool.pending if job[2][0] in adventure_ids]
    pool.pending = mine + [job for job in pool.pending if job not in mine]
    pool.run(len(mine))


def _refresh(live, client: TestClient) -> None:
    live.clock.advance(3600)
    client.post("/refresh")
    _settle(live)


def test_area_sets_accumulate_across_refreshes_and_lift_the_cap(live):
    live.server.queue("getuserdetails", _without(6), _without(12))
    client = live.start()  # first Refresh: only set 12
    _settle(live)

    before = client.get(PURE_ENERGY).text
    assert _verdict(before) == "Borderline" and rows(before, "push-cap")
    assert rows(before, "placement-hint")

    _refresh(live, client)  # second Refresh: only set 6

    after = client.get(PURE_ENERGY).text
    assert _verdict(after) == "Likely" and not rows(after, "push-cap")
    assert not rows(after, "placement-hint")
    morte = client.get(MORTE_FREE_PLAY).text  # set 12 is still known
    assert not rows(morte, "placement-hint")
    assert any("Area Set 12" in a for a in _assumptions(morte))


def test_the_area_set_cache_survives_a_restart(live):
    live.server.queue("getuserdetails", user_details(), _without(6, 12))
    client = live.start()
    _refresh(live, client)  # the newest snapshot has no area sets at all
    client.__exit__(None, None, None)

    restarted = live.start()
    _settle(live)

    assert _verdict(restarted.get(PURE_ENERGY).text) == "Likely"


def _seed_old_set_6(live) -> None:
    raw = read_responses(FIXTURE_LOG)
    old = {6: area_sets_from(raw, "1.200.0 (v600)")[6]}
    Store(live.data_dir).save_area_sets(old)


def test_a_verdict_on_an_area_set_seen_under_an_older_game_version_says_so(live):
    _seed_old_set_6(live)
    live.server.queue("getuserdetails", _without(6))
    client = live.start()
    _settle(live)

    page = client.get(PURE_ENERGY).text

    assert _verdict(page) == "Likely"  # no cap: only a Scoring Assumption
    assert (
        "Area Set 6 was last seen under game version 1.200.0 (v600) (now 1.242.1 (v659)): monster placement may "
        "be outdated." in _assumptions(page)
    )


def test_a_set_that_arrives_again_overwrites_the_old_copy(live):
    _seed_old_set_6(live)
    client = live.start()  # brings set 6 under the current version
    _settle(live)

    page = client.get(PURE_ENERGY).text

    assert not any("last seen under game version" in a for a in _assumptions(page))
    assert Store(live.data_dir).load_area_sets()[6].game_version == "1.242.1 (v659)"
