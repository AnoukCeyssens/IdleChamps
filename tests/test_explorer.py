"""Formation view (Explorer): adventure tree, completion badges, adventure header, seat grid.

The recorded log has no `getcampaigndetails` or `getcompletiondata`, so `progress.log` adds
small credential-free bodies in the shapes from issue #9 and Emmote's event-tier tab:
campaign 1 has Mad Wizard (7) and Towering Expectations (413) completed, the Running Event
(Feast of the Moon) has 2036 and 2037 completed, and Kitiara (180) finished event tier 1,
Avren (51) tier 3.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows

PROGRESS_LOG = Path(__file__).parent / "fixtures" / "progress.log"


@pytest.fixture(scope="module")
def explorer(tmp_path_factory) -> TestClient:
    responses = read_responses(FIXTURE_LOG) | read_responses(PROGRESS_LOG)
    log = tmp_path_factory.mktemp("explorer") / "requestlog.log"
    write_responses(responses, log)
    return TestClient(create_app(log))


def _tree(page: str) -> dict[str, dict]:
    return {row["text"].split(" · ")[0].removeprefix("↳ "): row for row in rows(page, "tree-adventure")}


def test_tree_lists_campaigns_and_events_from_the_catalogue(explorer):
    page = explorer.get("/adventures/7").text

    campaigns = [row["text"].split(" · ")[0] for row in rows(page, "tree-campaign")]

    assert "A Grand Tour of the Sword Coast" in campaigns
    assert "Feast of the Moon" in campaigns
    assert "Liar's Night" in campaigns
    # Dragon Heist has no adventures in the recorded definitions, so it isn't listed.
    assert "Waterdeep: Dragon Heist" not in campaigns


def test_tree_lists_adventures_with_their_variants(explorer):
    tree = _tree(explorer.get("/adventures/7").text)

    # 124 adventures in the recorded definitions.
    assert len(tree) == 124
    assert "variant" not in tree["The Mad Wizard"]["classes"]
    assert "variant" in tree["Pure Energy"]["classes"]
    assert "variant" in tree["Skittering Skulls"]["classes"]


def test_variants_sit_under_their_base_adventure(explorer):
    names = [row["text"].split(" · ")[0] for row in rows(explorer.get("/adventures/7").text, "tree-adventure")]

    morte = names.index("The Crypt of Legends (Morte)")
    assert names[morte + 1 : morte + 5] == [
        "↳ Skittering Skulls",
        "↳ Cacophony of Curses",
        "↳ Dodge This, Deaders!",
        "↳ Free Play (Morte)",
    ]


def test_completion_badges_done_best_area_and_not_run(explorer):
    tree = _tree(explorer.get("/adventures/7").text)

    # Completed per getcampaigndetails, with no best area recorded.
    assert tree["Towering Expectations"]["text"].endswith("Done")
    # Not in the completed list, but its best area (50) meets its objective (50).
    assert tree["The Mad Wizard"]["text"].endswith("Done")
    # Best area 84 of its objective 100.
    assert tree["Pure Energy"]["text"].endswith("Best area 84")
    assert tree["Elturel's Last Stand"]["text"].endswith("Not run")


def test_running_event_completion_comes_from_completed_adventure_ids(explorer):
    tree = _tree(explorer.get("/adventures/7").text)

    assert tree["Skittering Skulls"]["text"] == "↳ Skittering Skulls · Done"
    assert tree["Cacophony of Curses"]["text"] == "↳ Cacophony of Curses · Not run"
    assert "not-running" not in tree["Skittering Skulls"]["classes"]


def test_events_not_running_are_greyed_with_completion_unknown_and_tier(explorer):
    tree = _tree(explorer.get("/adventures/7").text)

    kitiara = tree["Conquest of the Blue Wing"]  # a Kitiara variant; Liar's Night isn't running
    assert "not-running" in kitiara["classes"]
    assert kitiara["text"].endswith("Not running · Completion unknown · Tier 1 done")
    assert tree["The Trickster's Delight (Avren)"]["text"].endswith("Completion unknown · Tier 3 done")
    # No tier recorded for Krond's champion.
    assert tree["Costumed Foes"]["text"].endswith("Not running · Completion unknown")


def test_without_completion_data_events_not_running_still_say_completion_unknown(client):
    tree = _tree(client.get("/adventures/7").text)

    assert tree["Conquest of the Blue Wing"]["text"].endswith("Not running · Completion unknown")


def test_adventure_of_an_event_not_running_can_still_be_opened(explorer):
    response = explorer.get("/adventures/2024")

    assert response.status_code == 200
    assert [r["text"] for r in rows(response.text, "header-badge")] == [
        "Not running",
        "Completion unknown · Tier 1 done",
    ]


def test_event_header_in_tree_says_whether_it_is_running(explorer):
    campaigns = {
        row["text"].split(" · ")[0]: row["text"] for row in rows(explorer.get("/adventures/7").text, "tree-campaign")
    }

    assert campaigns["Feast of the Moon"].endswith("Running")
    assert campaigns["Liar's Night"].endswith("Not running")


def _one(page: str, kind: str) -> str:
    (row,) = rows(page, kind)
    return row["text"]


def test_header_of_a_variant_shows_campaign_base_objective_and_restrictions(explorer):
    page = explorer.get("/adventures/2038").text

    assert _one(page, "header-campaign") == "Feast of the Moon"
    assert _one(page, "header-base") == "Variant of The Crypt of Legends (Morte)"
    assert _one(page, "header-objective") == "Objective: area 125"
    restrictions = _one(page, "header-restrictions")
    assert restrictions.startswith("Morte starts in the formation with Litany of Curses unlocked.")
    assert "Getting to Know Morte:" in restrictions
    assert "<b>" not in restrictions


def test_header_of_a_base_adventure_has_no_base(explorer):
    page = explorer.get("/adventures/7").text

    assert _one(page, "header-campaign") == "A Grand Tour of the Sword Coast"
    assert rows(page, "header-base") == []
    assert _one(page, "header-objective") == "Objective: area 50"
    assert _one(page, "header-restrictions") == "No restrictions"


def test_free_play_has_no_objective(explorer):
    assert _one(explorer.get("/adventures/2040").text, "header-objective") == "Free play: no objective"


def test_patron_chips_show_objective_area_and_locked_state(explorer):
    chips = [(r["text"], r["classes"]) for r in rows(explorer.get("/adventures/9").text, "patron-chip")]

    assert [text for text, _ in chips] == [
        "No patron",
        "Mirt · area 250 · locked",
        "Vajra · area 275 · locked",
        "Strahd · area 300 · locked",
        "Zariel · area 325 · locked",
        "Elminster · area 825 · locked",
    ]
    assert all("locked" in classes for _, classes in chips[1:])
    assert "on" in chips[0][1]


def test_choosing_a_patron_shows_its_objective(explorer):
    page = explorer.get("/adventures/9?patron=3").text

    assert _one(page, "header-objective") == "Objective: area 300 (Strahd)"
    assert [r["text"] for r in rows(page, "patron-chip") if "on" in r["classes"]] == ["Strahd · area 300 · locked"]


def test_adventure_without_patron_objectives_only_offers_no_patron(explorer):
    assert [r["text"] for r in rows(explorer.get("/adventures/2036").text, "patron-chip")] == ["No patron"]


def test_run_goal_tabs_are_push_gem_farm_and_gold_farm(explorer):
    tabs = rows(explorer.get("/adventures/7?goal=Gem Farm").text, "run-goal")

    assert [t["text"] for t in tabs] == ["Push", "Gem Farm", "Gold Farm"]
    assert [t["text"] for t in tabs if "on" in t["classes"]] == ["Gem Farm"]


def test_seat_grid_draws_the_real_layout_with_adjacency_and_front(explorer):
    page = explorer.get("/adventures/7").text  # Grand Tour's 9-slot Diamond Formation

    seats = rows(page, "seat")
    assert len(seats) == 9
    # Only slot 0 sits in the front column.
    front = [s["text"] for s in seats if "front" in s["classes"]]
    assert len(front) == 1 and front[0].startswith("Slot 0 ")
    # The Diamond's adjacency lists hold 16 distinct pairs.
    assert len(rows(page, "adjacency")) == 16
    assert len(rows(page, "front-marker")) == 1


def test_escort_blocked_slots_are_shown_as_blocked(explorer):
    seats = rows(explorer.get("/adventures/289").text, "seat")  # Inevitable Betrayal: escorts on 0 and 2

    assert len(seats) == 10
    assert [s["text"] for s in seats if "blocked" in s["classes"]] == [
        "Slot 0: blocked by an escort Slot 0 · blocked",
        "Slot 2: blocked by an escort Slot 2 · blocked",
    ]


def test_adventures_link_opens_the_first_adventure_in_the_tree(explorer):
    page = explorer.get("/champions").text
    assert 'href="/adventures"' in page

    response = explorer.get("/adventures")

    assert response.status_code == 200
    # Grand Tour is the first free play campaign; Mad Wizard (7) its first adventure.
    assert _one(response.text, "header-objective") == "Objective: area 50"
    assert "The Mad Wizard" in response.text.split("<h1>")[1].split("</h1>")[0]


def test_unknown_adventure_is_not_found(explorer):
    assert explorer.get("/adventures/999999").status_code == 404
