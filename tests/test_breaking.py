"""Area Set Cache and Armoured / Hits-based bosses in the Push search and verdict (#33).

Recording facts used: `getuserdetails.defines.adventure_area_defines` holds Area Sets 6 (The Mad
Wizard, Pure Energy) and 12 (the Crypt of Legends event adventures). Set 12's area-20 boss is the
hits-based Resurrected Hero of the Vale (monster 220, 50 hits); set 6 has no armoured or hits-based
monster. Pure Energy's estimate (~419) is far above its objective 100.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from advisor.request_log import read_responses, write_responses
from advisor.web import create_app
from conftest import FIXTURE_LOG, rows

PUSH = "/adventures/{}?goal=Push"


def _one(page: str, kind: str) -> str:
    (row,) = rows(page, kind)
    return row["text"]


def _assumptions(page: str) -> list[str]:
    return [r["text"] for r in rows(page, "assumption")]


def _app(tmp_path_factory, name, change) -> TestClient:
    responses = read_responses(FIXTURE_LOG)
    change(responses)
    log = tmp_path_factory.mktemp(name) / "requestlog.log.gz"
    write_responses(responses, log)
    return TestClient(create_app(log))


def _drop_area_set(responses, area_set_id):
    defines = responses["getuserdetails"]["defines"]
    defines["adventure_area_defines"] = [
        a for a in defines["adventure_area_defines"] if a["area_set_id"] != area_set_id
    ]


def test_known_placement_lifts_the_borderline_cap(client):
    page = client.get(PUSH.format(9)).text

    assert _one(page, "push-verdict") == "Likely"
    assert not rows(page, "push-cap")
    assert not rows(page, "placement-hint")
    assert any(a.startswith("Armoured and hits-based bosses checked") and "Area Set 6" in a for a in _assumptions(page))


@pytest.fixture(scope="module")
def no_set_6(tmp_path_factory) -> TestClient:
    return _app(tmp_path_factory, "no_set_6", lambda r: _drop_area_set(r, 6))


def test_unknown_placement_caps_and_hints_to_load_an_adventure_of_the_same_area_set(no_set_6):
    page = no_set_6.get(PUSH.format(9)).text

    assert _one(page, "push-verdict") == "Borderline"
    assert rows(page, "push-cap")
    assert _one(page, "placement-hint") == "Load Pure Energy in-game, then Refresh"
