"""The fixture builder turns a raw request log into a credential-free fixture log."""

import gzip
import json

from advisor.fixtures import build_fixture

SEPARATOR = "*" * 50
USER_ID = "7654321"
HASH = "deadbeefcafef00d1234"
INSTANCE_ID = 99887766
AUTH_TICKET = "1400AABBCCDDEEFF"


def _raw_log() -> str:
    """A tiny request log in the game's format, with credentials planted everywhere they occur."""
    blocks = [
        (
            "http://master.idlechampions.com/~idledragons/post.php?call=getPlayServerForDefinitions&network_id=11",
            {"success": True, "play_server": "https://ps1.idlechampions.com/~idledragons/"},
        ),
        (
            f"https://ps1.idlechampions.com/~idledragons/post.php?call=authenticatesteamuser&user_id={USER_ID}"
            f"&hash={HASH}&auth_ticket={AUTH_TICKET}&steam_name=Someone",
            {"success": True, "hash": HASH, "internal_user_id": USER_ID, "steam_user_id": "7656119"},
        ),
        (
            f"https://ps1.idlechampions.com/~idledragons/post.php?call=getuserdetails&user_id={USER_ID}&hash={HASH}",
            {
                "success": True,
                "linked_accounts": [{"network": "steam", "id": "7656119"}],
                "details": {
                    "instance_id": INSTANCE_ID,
                    "newsletter_email": "someone@example.com",
                    "heroes": [{"hero_id": "1", "level": "300", "owned": "1", "user_id": USER_ID}],
                    "modron_saves": {"1": {"core_id": 1, "user_id": USER_ID, "instance_id": str(INSTANCE_ID)}},
                    "trials_data": {"player_name": "Someone"},
                },
                "defines": {"campaign_defines": [{"id": 1, "name": "Grand Tour"}]},
            },
        ),
    ]
    out = [""]
    for url, body in blocks:
        out.append(f"{url}\n{json.dumps(body)}\n")
    return SEPARATOR.join(out)


def test_no_credential_survives_the_fixture_builder(tmp_path):
    raw = tmp_path / "webRequestLog.txt"
    raw.write_text(_raw_log(), encoding="utf-8")
    fixture = tmp_path / "fixture.log.gz"

    build_fixture(raw, fixture)

    text = gzip.decompress(fixture.read_bytes()).decode("utf-8")
    for secret in (USER_ID, HASH, str(INSTANCE_ID), AUTH_TICKET, "someone@example.com", "Someone", "7656119"):
        assert secret not in text
    for field in ("user_id", "hash", "instance_id", "auth_ticket", "newsletter_email", "player_name", "linked_accounts"):
        assert f'"{field}"' not in text and f"{field}=" not in text


def test_fixture_keeps_the_game_data(tmp_path):
    raw = tmp_path / "webRequestLog.txt"
    raw.write_text(_raw_log(), encoding="utf-8")
    fixture = tmp_path / "fixture.log.gz"

    build_fixture(raw, fixture)

    text = gzip.decompress(fixture.read_bytes()).decode("utf-8")
    assert "call=getuserdetails" in text
    assert '"hero_id": "1"' in text
    assert "Grand Tour" in text
    # Calls outside the read-only allow-list (here: the Steam login) are dropped entirely.
    assert "authenticatesteamuser" not in text
