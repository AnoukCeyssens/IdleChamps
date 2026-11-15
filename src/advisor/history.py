"""History Records: a slim, credential-free note of the account's progress at each Refresh.

Built from the responses a successful live Refresh leaves in the Store (already stripped of
credentials, shop and promo data). Nothing in v1 reads history; it is kept so that later
features can compare progress and rates over time (#20).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from advisor.account import Account, parse_account
from advisor.catalogue import game_version, parse_catalogue
from advisor.champion_models import ChampionModels
from advisor.push_verdict import instance_estimate
from advisor.request_log import RawResponses
from advisor.store import Store

HISTORY_VERSION = 1

_log = logging.getLogger(__name__)

_CONTRACT_BUFF_IDS = frozenset({31, 32, 33})  # gear contract buffs (#20); count = inventory_amount
_BEST_AREA_PREFIX = "highest_area_completed_ever_c"
_TIME_FIELDS = frozenset({"server_time"})


def write_history(store: Store, raw: RawResponses) -> None:
    """The History Record writer: called after each successful live Refresh. Skips the record
    when nothing but the time changed since the last one."""
    # The Refresh itself succeeded: a lost History Record must not turn it into an error.
    try:
        definitions = store.load_definitions()
        history = store.load_history()
        last = history[-1] if history else None
        record = history_record(raw, definitions.checksum if definitions else None, last)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        _log.warning("History Record skipped: the snapshot couldn't be read (%s: %s)", type(error).__name__, error)
        return
    if last is not None and _sans_time(last) == _sans_time(record):
        return
    try:
        store.append_history(record)
    except OSError as error:
        _log.warning("History Record not saved: %s", error)


def _sans_time(record: Mapping[str, Any]) -> Any:
    kept = {k: v for k, v in record.items() if k not in _TIME_FIELDS and k != "owned_state"}
    return json.loads(json.dumps(kept, sort_keys=True))


def history_record(
    raw: RawResponses, definitions_checksum: str | None, previous: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The record for one Refresh. The owned-state subset rides along only when the
    recommendation key differs from `previous` (the last record written)."""
    user = raw["getuserdetails"]
    details = user["details"]
    server_time = _int(user.get("current_time"))
    account = parse_account(raw)
    record = {
        "version": HISTORY_VERSION,
        "server_time": server_time,
        "game_version": game_version(raw),
        "definitions_checksum": definitions_checksum,
        "totals": _totals(details),
        "instances": [_instance(i, server_time) for i in details.get("game_instances") or ()],
        "recommendation_key": account.recommendation_key,
    }
    estimates = _push_estimates(raw, account)
    for instance in record["instances"]:
        instance["push_estimate"] = estimates.get(instance["game_instance_id"])
    if previous is None or previous.get("recommendation_key") != record["recommendation_key"]:
        record["owned_state"] = account.owned_state()
    return record


def _totals(details: dict) -> dict[str, Any]:
    stats = details.get("stats") or {}
    return {
        "red_rubies": _int(details.get("red_rubies")),
        "red_rubies_spent": _int(details.get("red_rubies_spent")),
        "contracts": {
            str(b["buff_id"]): _int(b.get("inventory_amount"))
            for b in details.get("buffs") or ()
            if _int(b.get("buff_id")) in _CONTRACT_BUFF_IDS
        },
        "chests": {str(k): _int(v) for k, v in (details.get("chests") or {}).items()},
        "reset_currencies": {
            str(c["currency_id"]): _number(c.get("total_earned")) for c in details.get("reset_currencies") or ()
        },
        "resets": _int(stats.get("resets")),
        "seconds_played": _int(details.get("seconds_played")),
        "highest_areas": {
            k.removeprefix(_BEST_AREA_PREFIX): _int(v) for k, v in stats.items() if k.startswith(_BEST_AREA_PREFIX)
        },
    }


def _instance(instance: dict, server_time: int | None) -> dict[str, Any]:
    stats = instance.get("stats") or {}
    since_reset = _int(instance.get("seconds_since_reset"))
    return {
        "game_instance_id": _int(instance.get("game_instance_id")),
        "adventure_id": _int(instance.get("current_adventure_id")),
        "patron_id": _int(instance.get("current_patron_id")),
        "current_area": _int(instance.get("current_area")),
        "highest_area": _int(instance.get("highest_area")),
        # stored as a moment rather than a duration, so an unchanged run reads as unchanged
        "run_started_at": server_time - since_reset if None not in (server_time, since_reset) else None,
        "gems_earned": _int(stats.get("this_reset_gems_earned")),
        "highest_damage_dealt": _number(stats.get("this_reset_highest_damage_dealt")),
        "top_damage_champion_id": _int(stats.get("this_reset_highest_damage_dealt_hero_id")),
        "resets_since_last_manual": _int((instance.get("auto_reset_stats") or {}).get("resets_since_last_manual")),
        "formation": [_int(c) for c in instance.get("formation") or ()],  # champion id per slot, -1 empty
        "push_estimate": None,  # the Push estimate for the formation in play, filled by `_push_estimates`
    }


def _push_estimates(raw: RawResponses, account: Account) -> dict[int, dict[str, Any]]:
    """The Push estimate for each running instance's formation in play, by instance id (none when an
    instance can't be estimated, e.g. no BUD or an unhandled adventure modifier)."""
    try:
        catalogue = parse_catalogue(raw)
    except (KeyError, TypeError, ValueError):
        return {}
    models = ChampionModels(catalogue, account)
    estimates = {}
    for instance in account.instances:
        verdict = instance_estimate(catalogue, account, models, instance)
        if verdict is not None:
            estimates[instance.id] = {
                "expected_area": verdict.expected_area,
                "area": verdict.area,
                "band": verdict.band,
                "objective": verdict.objective,
                "verdict": verdict.label,
            }
    return estimates


def _number(value: Any) -> float | None:
    """Big numbers arrive as sci-notation strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
