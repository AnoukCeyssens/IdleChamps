"""Live Refresh through the app, against a fake game server replaying recorded responses."""

from __future__ import annotations

import gzip
import threading

import pytest
from fastapi.testclient import TestClient

from advisor.game_client import READ_ONLY_CALLS, CallNotAllowed, LiveGameClient
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows
from fake_game import PLAY_SERVER, definitions, unreachable


def test_startup_refresh_makes_the_read_only_sequence(live):
    live.start()

    assert live.server.calls() == [
        "getplayserverfordefinitions",
        "getuserdetails",
        "getcampaigndetails",
        "getpatrondetails",
        "geteventsdetails",
        "getcompletiondata",
        "getdefinitions",
    ]
    assert live.server.requests[0].url.startswith("https://master.idlechampions.com/")
    assert all(r.url.startswith(PLAY_SERVER) for r in live.server.requests[1:])


def test_startup_refresh_shows_the_live_account(live):
    page = live.start().get("/champions").text

    assert len(rows(page, "champion")) == 168
    assert "Level 300" in next(r["text"] for r in rows(page, "champion") if r["text"].startswith("Bruenor"))


def _header(page: str) -> tuple[str, dict]:
    [state] = rows(page, "refresh-state")
    [button] = rows(page, "refresh-button")
    return state["text"], button


def test_header_shows_how_long_ago_the_refresh_was(live):
    client = live.start()
    live.clock.advance(5)

    state, _ = _header(client.get("/champions").text)

    assert state == "refreshed 5 s ago"


def test_button_shows_the_cooldown_and_ignores_clicks_during_it(live):
    client = live.start()
    live.clock.advance(15)

    page = client.post("/refresh", headers={"referer": "http://testserver/champions?role=dps"}).text

    _, button = _header(page)
    assert button["text"] == "Refresh in 45 s"
    assert live.server.calls().count("getuserdetails") == 1  # the click was ignored
    assert len(rows(page, "champion")) == 49  # back on the page the click came from


def test_after_the_cooldown_a_click_refreshes_again_without_rediscovering_the_server(live):
    client = live.start()
    live.clock.advance(61)
    _, button = _header(client.get("/champions").text)
    assert button["text"] == "Refresh"

    page = client.post("/refresh").text

    assert live.server.calls().count("getuserdetails") == 2
    assert live.server.calls().count("getplayserverfordefinitions") == 1  # cached for the session
    state, button = _header(page)
    assert state == "refreshed 0 s ago"
    assert button["text"] == "Refresh in 60 s"


def test_calls_are_sent_one_at_a_time_at_least_200_ms_apart(live):
    live.start()

    times = [r.at for r in live.server.requests]
    assert all(later - earlier >= 0.2 - 1e-9 for earlier, later in zip(times, times[1:]))


def test_a_flaky_call_is_retried_at_most_four_times(live):
    live.server.queue("getcampaigndetails", *[unreachable()] * 4)

    page = live.start().get("/champions").text

    assert live.server.calls().count("getcampaigndetails") == 5
    assert rows(page, "refresh-failed") == []
    assert len(rows(page, "champion")) == 168


def test_a_call_that_keeps_failing_gives_up_after_four_retries(live):
    live.server.queue("getpatrondetails", *[unreachable()] * 10)

    page = live.start().get("/champions").text

    assert live.server.calls().count("getpatrondetails") == 5
    assert "getdefinitions" not in live.server.calls()
    [banner] = rows(page, "refresh-failed")
    assert "can't be reached" in banner["text"]


def test_switch_play_server_is_followed(live):
    live.server.queue(
        "getuserdetails", {"success": True, "switch_play_server": "http://ps22.idlechampions.com/~idledragons/"}
    )

    page = live.start().get("/champions").text

    after_switch = live.server.requests[2:]
    assert [r.call for r in live.server.requests[:3]] == [
        "getplayserverfordefinitions",
        "getuserdetails",
        "getuserdetails",
    ]
    assert all(r.url.startswith("https://ps22.idlechampions.com/~idledragons/") for r in after_switch)
    assert len(rows(page, "champion")) == 168


def test_outdated_instance_id_fetches_user_details_again_and_retries(live):
    live.server.queue("getpatrondetails", {"success": False, "failure_reason": "Outdated instance id"})

    page = live.start().get("/champions").text

    assert live.server.calls()[:7] == [
        "getplayserverfordefinitions",
        "getuserdetails",
        "getcampaigndetails",
        "getpatrondetails",
        "getuserdetails",
        "getpatrondetails",
        "geteventsdetails",
    ]
    assert rows(page, "refresh-failed") == []


def _definitions_requests(live):
    return [r for r in live.server.requests if r.call == "getdefinitions"]


def test_definitions_are_downloaded_in_full_once_then_only_when_the_checksum_changed(live):
    client = live.start()
    [first] = _definitions_requests(live)
    assert "checksum" not in first.params

    # Unchanged checksum: the server sends no tables, and the cached definitions still serve.
    live.clock.advance(61)
    page = client.post("/refresh").text

    second = _definitions_requests(live)[1]
    assert second.params["checksum"] == "1727461343"
    assert len(rows(page, "champion")) == 168

    # Changed checksum: only the changed table arrives and is merged into the cache.
    renamed = definitions()
    for hero in renamed["hero_defines"]:
        if hero["name"] == "Bruenor":
            hero["name"] = "Bruenor Battlehammer"
    live.server.queue(
        "getdefinitions", {"success": True, "checksum": 42, "hero_defines": renamed["hero_defines"]}
    )
    live.clock.advance(61)
    page = client.post("/refresh").text

    names = [r["text"].split(" · ")[0] for r in rows(page, "champion")]
    assert "Bruenor Battlehammer" in names
    assert len(names) == 168


def test_failed_refresh_shows_cause_and_fix_and_keeps_the_cached_data(live):
    client = live.start()
    live.server.queue("getuserdetails", {"success": False, "failure_reason": "Security hash failure"})
    live.clock.advance(61)

    page = client.post("/refresh").text

    [banner] = rows(page, "refresh-failed")
    assert "stale" in banner["text"]
    assert "Launch Idle Champions once, then Refresh." in banner["text"]
    assert len(rows(page, "champion")) == 168  # the last snapshot stays visible
    state, button = _header(page)
    assert state == "refreshed 61 s ago"
    assert button["text"] == "Refresh"  # no cooldown after a failure
    assert live.server.calls().count("getuserdetails") == 2  # a stale hash is never retried


def test_a_later_successful_refresh_clears_the_banner(live):
    live.server.queue("getuserdetails", {"success": False, "failure_reason": "Security hash failure"})
    client = live.start()

    page = client.post("/refresh").text

    assert rows(page, "refresh-failed") == []
    assert len(rows(page, "champion")) == 168


def test_without_a_login_in_the_request_log_the_banner_says_to_launch_the_game(live):
    live.log.write_text("")  # the game never launched (or its log was cleared)

    page = live.start().get("/champions").text

    [banner] = rows(page, "refresh-failed")
    assert "Launch Idle Champions once, then Refresh." in banner["text"]
    assert live.server.requests == []


def test_failed_first_refresh_never_falls_back_to_the_request_log(live, tmp_path):
    # The request log holds a login and full response bodies; live mode must not show them.
    recorded = gzip.decompress(FIXTURE_LOG.read_bytes()).decode("utf-8")
    live.log.write_text(live.log.read_text() + recorded)
    assert len(rows(TestClient(create_app(live.log)).get("/champions").text, "champion")) == 168
    live.server.queue("getplayserverfordefinitions", *[unreachable()] * 10)

    page = live.start().get("/champions").text

    assert rows(page, "refresh-failed") != []
    assert rows(page, "champion") == []
    assert _header(page)[0] == "Not refreshed yet"


def test_clicks_while_a_refresh_runs_are_ignored(live):
    client = live.start()
    live.clock.advance(61)
    in_flight, release = threading.Event(), threading.Event()

    def hold(call):
        if call == "getuserdetails":
            in_flight.set()
            release.wait(5)

    live.server.on_get = hold
    first = threading.Thread(target=client.post, args=("/refresh",))
    first.start()
    assert in_flight.wait(5)

    page = client.post("/refresh").text  # a second click while the first runs
    state, button = _header(page)
    release.set()
    first.join(5)

    assert state == "Refreshing…"
    assert button["text"] == "Refreshing…"
    assert live.server.calls().count("getuserdetails") == 2  # startup + the first click only


def test_only_allow_listed_read_calls_can_be_sent(live):
    game = LiveGameClient(live.server, live.log, clock=live.clock, sleep=live.clock.sleep)

    for write_call in ("saveformation", "SaveModron", "softreset", "purchasefeat", "redeemcoupon", "opengenericchest"):
        with pytest.raises(CallNotAllowed):
            game.get(PLAY_SERVER, write_call, {})

    assert live.server.requests == []
    live.start()
    assert {r.call for r in live.server.requests} <= READ_ONLY_CALLS


def test_startup_refresh_runs_in_the_background_and_the_header_reloads_the_page_when_it_lands(live):
    jobs = []
    client = live.start(background=jobs.append)

    page = client.get("/champions").text
    assert _header(page)[0] == "Not refreshed yet"
    assert rows(page, "champion") == []
    assert 'hx-get="/header?seen=0"' in page

    [startup_refresh] = jobs
    startup_refresh()

    polled = client.get("/header", params={"seen": 0})
    assert polled.headers.get("HX-Refresh") == "true"
    assert "refreshed 0 s ago" in polled.text
    assert "HX-Refresh" not in client.get("/header", params={"seen": 1}).headers
    assert len(rows(client.get("/champions").text, "champion")) == 168


def test_offline_mode_header_has_no_refresh(client):
    page = client.get("/champions").text

    assert rows(page, "refresh-state")[0]["text"] == "Offline mode: reading the game's request log"
    assert rows(page, "refresh-button") == []
