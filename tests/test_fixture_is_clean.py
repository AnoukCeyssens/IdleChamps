"""The committed fixture log carries no credential field anywhere."""

import json

from advisor.request_log import CREDENTIAL_FIELDS, read_responses
from conftest import FIXTURE_LOG


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def test_committed_fixture_has_no_credential_field():
    responses = read_responses(FIXTURE_LOG)

    assert set(responses) >= {"getuserdetails", "getdefinitions"}
    assert not CREDENTIAL_FIELDS & set(_keys(responses))
    for field in ("user_id", "hash", "instance_id"):
        assert f'"{field}"' not in json.dumps(responses)
