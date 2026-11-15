"""Area Sets: which monster stands at which area, and which bosses are Armoured or Hits-based (#17-#19).

- **Area Set**: one `area_set_id`'s `adventure_area_defines` (50 areas that loop) with the monster
  definitions they name. They only arrive in `getuserdetails.defines`, for the adventures loaded in
  the player's game instances, so the **Area Set Cache** keeps every set it ever sees: a set that
  arrives again overwrites the old copy, nothing expires, and each set notes the game version it was
  last seen under. The Store persists it (`area_sets.json.gz`).
- **Detection** (`boss_health`): a boss is Armoured or Hits-based when its flag
  (`properties.armor_based_damage` / `hits_based_damage`) or its tag (`armor_based` / `hits_based`)
  says so; then the adventure's (variant's) `game_changes` apply on top. Segments = the monster's
  `override_health_multiplier`, else its `health_modifier`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

AREA_SETS_VERSION = 1
BOSS_HEALTH = 50.0  # a boss's health_modifier when its definition doesn't say

# `game_changes` types that change Armoured / Hits-based health (handled here, not by the damage estimate).
BREAKING_CHANGES = {"set_monster_health_type", "override_armor_based_min_health_multiplier"}
# Monster properties that only concern breaking (`add_monster_properties*`).
BREAKING_PROPERTIES = {"armor_based_damage", "hits_based_damage", "first_health_ranges", "override_health_multiplier"}


@dataclass(frozen=True)
class AreaSet:
    id: int
    areas: Mapping[int, Mapping[str, Any]] = field(hash=False)  # area_id (1..) -> its `adventure_area_defines` row
    monsters: Mapping[int, Mapping[str, Any]] = field(hash=False)  # monster id -> definition (as last seen)
    game_version: str | None = None

    @property
    def length(self) -> int:
        return max(self.areas, default=0)

    def bosses(self, area: int) -> tuple[Mapping[str, Any], ...]:
        """The bosses (monster type 2) in `area`'s waves; area sets loop past their last area."""
        if not self.length:
            return ()
        define = self.areas.get((area - 1) % self.length + 1) or {}
        ids = [m for wave in define.get("waves") or () for m in wave]
        found = (self.monsters.get(int(m)) for m in dict.fromkeys(ids))
        return tuple(m for m in found if m and int(m.get("type") or 0) == 2)


@dataclass(frozen=True)
class BossHealth:
    kind: str  # "armoured" | "hits" | "normal"
    segments: float  # chunks (armoured) or hits (hits-based); 1 for normal
    health_modifier: float  # × a normal monster's health


def area_sets_from(raw: Mapping[str, Any], game_version: str | None) -> dict[int, AreaSet]:
    """The Area Sets in one `getuserdetails` response."""
    defines = (raw.get("getuserdetails") or {}).get("defines") or {}
    monsters = {int(m["id"]): _monster(m) for m in defines.get("monster_defines") or () if "id" in m}
    rows: dict[int, dict[int, dict]] = {}
    for row in defines.get("adventure_area_defines") or ():
        if row.get("area_set_id") is None or row.get("area_id") is None:
            continue
        rows.setdefault(int(row["area_set_id"]), {})[int(row["area_id"])] = {
            k: row[k] for k in ("area_id", "waves", "monsters") if k in row
        }
    sets = {}
    for set_id, areas in rows.items():
        named = {int(m) for a in areas.values() for w in a.get("waves") or () for m in w}
        named |= {int(m) for a in areas.values() for m in a.get("monsters") or ()}
        sets[set_id] = AreaSet(set_id, areas, {i: monsters[i] for i in sorted(named) if i in monsters}, game_version)
    return sets


def merge(cache: Mapping[int, AreaSet], arrived: Mapping[int, AreaSet]) -> dict[int, AreaSet]:
    """The cache with every arriving set overwriting its old copy; nothing is ever dropped."""
    return {**cache, **arrived}


def to_json(cache: Mapping[int, AreaSet]) -> dict:
    return {
        "version": AREA_SETS_VERSION,
        "sets": {
            str(s.id): {
                "game_version": s.game_version,
                "areas": {str(k): v for k, v in s.areas.items()},
                "monsters": {str(k): v for k, v in s.monsters.items()},
            }
            for s in cache.values()
        },
    }


def from_json(data: Mapping[str, Any] | None) -> dict[int, AreaSet]:
    if not data or data.get("version") != AREA_SETS_VERSION:
        return {}
    return {
        int(k): AreaSet(
            int(k),
            {int(a): v for a, v in (s.get("areas") or {}).items()},
            {int(m): v for m, v in (s.get("monsters") or {}).items()},
            s.get("game_version"),
        )
        for k, s in (data.get("sets") or {}).items()
    }


def boss_health(monster: Mapping[str, Any], game_changes: tuple = ()) -> BossHealth:
    """Is this boss Armoured or Hits-based in this adventure, and with how many segments?"""
    props = monster.get("properties") or {}
    tags = set(monster.get("tags") or ())
    modifier = _number(monster.get("health_modifier")) or BOSS_HEALTH
    kind = (
        "armoured" if props.get("armor_based_damage") or "armor_based" in tags
        else "hits" if props.get("hits_based_damage") or "hits_based" in tags
        else "normal"
    )
    segments = _number(props.get("override_health_multiplier")) or modifier
    for change in game_changes:
        if not isinstance(change, dict):
            continue
        kind_of_change = change.get("type")
        if kind_of_change == "set_monster_health_type" and _targets_boss(change, tags):
            health_type = str(change.get("health_type") or "")
            if health_type in ("armor_based", "hits_based"):
                kind = "armoured" if health_type == "armor_based" else "hits"
                segments = _number(change.get("amount")) or segments
        elif str(kind_of_change).startswith("add_monster_properties") and change.get("affects_bosses", True) is not False:
            for added in _property_groups(change, int(monster.get("id") or 0), tags):
                if added.get("armor_based_damage"):
                    kind = "armoured"
                if added.get("hits_based_damage"):
                    kind = "hits"
                if kind != "normal" and _number(added.get("override_health_multiplier")):
                    segments = _number(added["override_health_multiplier"])
                for layer in added.get("first_health_ranges") or ():
                    if isinstance(layer, dict) and layer.get("type") == "armor_based":
                        kind, segments = "armoured", _number(layer.get("armor_chunks")) or segments
        elif kind_of_change == "override_armor_based_min_health_multiplier" and kind == "armoured":
            least = _number(change.get("amount") or change.get("value") or change.get("multiplier"))
            segments = max(segments, least or 0)
    return BossHealth(kind, segments if kind != "normal" else 1.0, modifier)


def _monster(define: Mapping[str, Any]) -> dict:
    return {k: define[k] for k in ("id", "name", "type", "tags", "health_modifier", "properties") if k in define}


def _targets_boss(change: Mapping, tags: set[str]) -> bool:
    target_class = str(change.get("target_class") or "all").lower()
    if target_class not in ("all", "boss", "bosses", "any"):
        return False
    wanted = change.get("target_tags")
    return not wanted or any(_tag_matches(str(t), tags) for t in wanted)


def _property_groups(change: Mapping, monster_id: int, tags: set[str]):
    if isinstance(change.get("properties"), dict):
        yield change["properties"]
    for expr, props in (change.get("by_tag") or {}).items():
        if isinstance(props, dict) and _tag_matches(str(expr), tags):
            yield props
    for mid, props in (change.get("by_id") or {}).items():
        if isinstance(props, dict) and str(mid) == str(monster_id):
            yield props


def _tag_matches(expr: str, tags: set[str]) -> bool:
    """`a^b` = a and b; `!a` = not a (e.g. `!hits_based^!armor_based`)."""
    return all((t[1:] not in tags) if t.startswith("!") else (t in tags) for t in expr.split("^") if t)


def _number(value) -> float | None:
    try:
        return float(value) if value is not None and float(value) > 0 else None
    except (TypeError, ValueError):
        return None
