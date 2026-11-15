"""Recompute (#30): after each Refresh every adventure × Run Goal is precomputed in a background
pool, keyed on the recommendation key and the definitions checksum.

The pool is `fake_game.ManualPool`: jobs run in this process, only when the test says so, so a
test can look at the app halfway through a recompute. Most tests serve a getuserdetails trimmed
to a few adventures (`_small`), so running a whole recompute stays quick.
"""

from __future__ import annotations

import pytest

from conftest import rows
from fake_game import RECOMPUTE, definitions, user_details

GOLD = "/adventures/{}?goal=Gold%20Farm"
KEPT = (7, 9, 44, 45)  # adventures kept in the trimmed snapshot


def _small(change=lambda details: None) -> dict:
    body = user_details()
    defines = body["defines"]
    defines["adventure_defines"] = [a for a in defines["adventure_defines"] if int(a["id"]) in KEPT]
    change(body["details"])
    return body


@pytest.fixture
def client(live):
    """Live mode on the trimmed snapshot, after its startup Refresh."""
    live.server.queue("getuserdetails", _small())
    return live.start()


def _progress(page: str) -> str | None:
    found = rows(page, "recompute-progress")
    return found[0]["text"] if found else None


def _refresh_with(live, client, change=lambda details: None):
    """A Refresh 61 s later whose (trimmed) getuserdetails carries `change`."""
    live.server.queue("getuserdetails", _small(change))
    live.clock.advance(61)
    return client.post("/refresh")


def _more_gems(details):
    details["red_rubies"] = int(details.get("red_rubies") or 0) + 1000


def _level_up(details):
    hero = next(h for h in details["heroes"] if int(h["level"]) > 0)
    hero["level"] = str(int(hero["level"]) + 1)


def test_a_refresh_queues_every_adventure_including_events_not_running(live):
    client = live.start()
    [pool] = live.pools

    assert len(pool.jobs(RECOMPUTE)) == 124  # every adventure in the recording, one job each (all Run Goals)
    assert _progress(client.get("/champions").text) == "Recomputing 0/124 adventures"


def test_progress_is_shown_in_the_header_until_the_recompute_is_done(live, client):
    [pool] = live.pools
    assert _progress(client.get("/champions").text) == "Recomputing 0/4 adventures"

    pool.run(1, RECOMPUTE)
    assert _progress(client.get("/champions").text) == "Recomputing 1/4 adventures"

    pool.run(kind=RECOMPUTE)
    page = client.get(GOLD.format(7)).text
    assert _progress(page) is None
    assert rows(page, "main-dps")[0]["text"].startswith("Main DPS:")


def test_a_currency_only_refresh_does_not_recompute(live, client):
    live.pools[0].run(kind=RECOMPUTE)

    page = _refresh_with(live, client, _more_gems).text

    assert rows(page, "refresh-state")[0]["text"] == "refreshed 0 s ago"
    assert len(live.pools) == 1  # no new recompute
    assert _progress(page) is None
    page = client.get(GOLD.format(7)).text
    assert rows(page, "main-dps")
    assert rows(page, "stale-result") == []


def test_old_results_stay_visible_marked_stale_while_a_recompute_runs(live, client):
    live.pools[0].run(kind=RECOMPUTE)
    before = rows(client.get(GOLD.format(7)).text, "main-dps")

    page = _refresh_with(live, client, _level_up).text

    assert _progress(page) == "Recomputing 0/4 adventures"
    page = client.get(GOLD.format(7)).text
    assert rows(page, "main-dps") == before
    [stale] = rows(page, "stale-result")
    assert "from before your last Refresh" in stale["text"]

    live.pools[1].run(kind=RECOMPUTE)
    page = client.get(GOLD.format(7)).text
    assert rows(page, "stale-result") == []
    assert rows(page, "main-dps")


def test_the_header_reloads_the_page_when_a_recompute_finishes(live, client):
    assert "hx-vals='{\"computed\": 0}'" in client.get("/champions").text

    assert "HX-Refresh" not in client.get("/header", params={"seen": 1, "computed": 0}).headers
    live.pools[0].run(kind=RECOMPUTE)
    assert client.get("/header", params={"seen": 1, "computed": 0}).headers.get("HX-Refresh") == "true"
    assert "HX-Refresh" not in client.get("/header", params={"seen": 1, "computed": 1}).headers


def test_a_key_change_mid_recompute_cancels_it_and_restarts_from_the_new_snapshot(live, client):
    first = live.pools[0]
    first.run(1, RECOMPUTE)

    page = _refresh_with(live, client, _level_up).text

    assert first.shut_down and first.pending == []  # pending jobs cancelled (Next Steps' too)
    [_, second] = live.pools  # the harness fails if a pool starts before the old one is shut down
    assert _progress(page) == "Recomputing 0/4 adventures"
    second.run(kind=RECOMPUTE)
    assert _progress(client.get("/champions").text) is None


def test_results_are_reused_after_a_restart_when_key_and_checksum_match(live, client):
    live.pools[0].run(kind=RECOMPUTE)
    before = client.get(GOLD.format(7)).text

    client.__exit__(None, None, None)  # the app stops
    live.server.queue("getuserdetails", _small(_more_gems))
    restarted = live.start()  # a new app on the same data folder; its startup Refresh succeeds

    assert not any(pool.jobs(RECOMPUTE) for pool in live.pools[1:])  # nothing recomputed
    page = restarted.get(GOLD.format(7)).text
    assert rows(page, "main-dps") == rows(before, "main-dps")
    assert rows(page, "seat-row") == rows(before, "seat-row")
    assert rows(page, "left-out") == rows(before, "left-out")


def test_a_definitions_change_recomputes_after_a_restart(live, client):
    live.pools[0].run(kind=RECOMPUTE)

    changed = definitions()
    changed["checksum"] = "a-newer-checksum"
    live.server.queue("getdefinitions", changed)
    live.server.queue("getuserdetails", _small())
    client.__exit__(None, None, None)
    live.start()

    assert len(live.pools[-1].jobs(RECOMPUTE)) == 4


def _no_runs_started(details):
    for instance in details["game_instances"]:
        instance.get("stats", {}).pop("this_reset_highest_damage_dealt", None)


def test_the_push_verdict_follows_my_runs_without_recomputing_formations(live):
    """Starting a Push run changes no recommendation key (runs aren't part of it), but the Push
    Verdict's Anchor Run comes from the runs: it must not stay "No estimate"."""
    live.server.queue("getuserdetails", _small(_no_runs_started))
    client = live.start()
    live.pools[0].run(kind=RECOMPUTE)
    assert rows(client.get("/adventures/9?goal=Push").text, "push-verdict")[0]["text"] == (
        "No estimate: start any Push run and Refresh"
    )

    _refresh_with(live, client)  # the same account, now with runs that have a BUD

    page = client.get("/adventures/9?goal=Push").text
    assert len(live.pools) == 1  # no recompute
    assert rows(page, "push-verdict")[0]["text"] != "No estimate: start any Push run and Refresh"
    assert rows(page, "push-estimate")
