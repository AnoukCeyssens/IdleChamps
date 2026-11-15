"""Champions list: every champion, owned ones with their level, unowned greyed out with their source."""

from conftest import rows


def _by_name(page: str) -> dict[str, dict]:
    return {row["text"].split(" · ")[0]: row for row in rows(page, "champion")}


def test_lists_every_champion_in_the_game_definitions(client):
    page = client.get("/champions").text

    champions = _by_name(page)

    # The recorded Game Definitions hold 168 real champions (plus 11 "Please Reload" placeholders).
    assert len(rows(page, "champion")) == 168
    assert "Please Reload" not in champions


def test_owned_champion_shows_its_level(client):
    champions = _by_name(client.get("/champions").text)

    bruenor = champions["Bruenor"]
    assert "unowned" not in bruenor["classes"]
    assert "Level 300" in bruenor["text"]


def test_unowned_event_champion_is_greyed_out_with_event_and_year(client):
    champions = _by_name(client.get("/champions").text)

    strix = champions["Strix"]
    assert "unowned" in strix["classes"]
    assert "Festival of Fools, Year 1" in strix["text"]
    assert "Liar's Night, Year 10" in champions["Kitiara"]["text"]


def test_unowned_evergreen_champion_comes_from_chests(client):
    champions = _by_name(client.get("/champions").text)

    drizzt = champions["Drizzt"]
    assert "unowned" in drizzt["classes"]
    assert "Chest" in drizzt["text"]
