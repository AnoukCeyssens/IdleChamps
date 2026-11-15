"""Role-tag filter on the Champions list."""

from conftest import rows


def _names(page: str) -> set[str]:
    return {row["text"].split(" · ")[0] for row in rows(page, "champion")}


def test_filter_keeps_only_champions_with_that_role_tag(client):
    page = client.get("/champions", params={"role": "dps"}).text

    names = _names(page)
    # 49 champions carry the `dps` tag in the recorded Game Definitions.
    assert len(names) == 49
    assert {"Strix", "Drizzt", "Asharra"} <= names  # unowned event, unowned chest, owned
    assert "Bruenor" not in names  # support only


def test_filter_offers_every_role_tag(client):
    page = client.get("/champions").text

    offered = {row["text"] for row in rows(page, "role-filter")}
    assert offered == {"All", "dps", "support", "healing", "tanking", "gold", "speed", "control", "debuff", "breaking"}


def test_htmx_filter_swaps_in_just_the_list(client):
    response = client.get("/champions", params={"role": "breaking"}, headers={"HX-Request": "true"})

    assert "<html" not in response.text
    assert "Kitiara" in _names(response.text)
    assert len(rows(response.text, "champion")) == 9


def test_unknown_role_shows_everyone(client):
    assert len(rows(client.get("/champions", params={"role": "nonsense"}).text, "champion")) == 168
