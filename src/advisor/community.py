"""Curated community formations (spec #22, #2, #5): vendored, pinned snapshots parsed as data.

`curated/rules.toml` lists each source (repo, pinned commit, vendored file, licence) and how to read
it. Today that is Emmote's Gem Farm example formations (`formsData` in Emmotes/ic_scripting_routes'
forms.js, MIT). The JS file is never executed: the `formsData` object literal is read as JSON.

A community formation matches an adventure when its campaign is the adventure's (by name) and its
slot positions are the adventure's layout (same shape, slot for slot). Matching formations seed the
Formation Search and are compared with the recommendation; they never override it.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from advisor.catalogue import Adventure, Catalogue

CURATED = Path(__file__).parent / "curated"
_SHAPE_TOLERANCE = 0.02  # normalised slot positions this close count as the same slot


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    repo: str
    commit: str
    path: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/blob/{self.commit}/{self.path}"


@dataclass(frozen=True)
class CommunityFormation:
    name: str
    source: Source
    run_goal: str
    campaign_name: str
    positions: tuple[tuple[float, float], ...]  # per slot index, the source's own coordinates
    formation: tuple[int, ...]  # champion id per slot index, 0 = empty
    specializations: dict[int, tuple[int, ...]]  # champion id -> specialization upgrade ids


@cache
def community_formations() -> tuple[CommunityFormation, ...]:
    rules = tomllib.loads((CURATED / "rules.toml").read_text("utf-8"))
    found = []
    for entry in rules.get("source") or ():
        source = Source(entry["id"], entry["title"], entry["repo"], entry["commit"], entry["path"])
        if entry.get("format") != "emmotes-formsData":
            raise ValueError(f"unknown curated source format: {entry.get('format')}")
        data = _object_literal((CURATED / entry["file"]).read_text("utf-8"), "formsData")
        found.extend(_emmotes_forms(source, entry, data))
    return tuple(found)


def matching(catalogue: Catalogue, adventure: Adventure, run_goal: str) -> tuple[CommunityFormation, ...]:
    """The community formations for this adventure (its campaign and layout) and Run Goal."""
    campaign = catalogue.campaign(adventure.campaign_id)
    name = _campaign_key(campaign.name) if campaign else None
    layout = tuple((s.x, s.y) for s in sorted(adventure.layout, key=lambda s: s.index))
    return tuple(
        f for f in community_formations()
        if f.run_goal == run_goal and _campaign_key(f.campaign_name) == name and _same_shape(f.positions, layout)
    )


def _emmotes_forms(source: Source, entry: dict, data: dict):
    which = entry.get("formation", "Q")
    types, variants = entry.get("types") or {}, entry.get("variants") or {}
    for campaign in data.values():
        positions = tuple((float(s["x"]), float(s["y"])) for s in campaign.get("slots") or ())
        for type_id, by_widdle in (campaign.get("forms") or {}).items():
            for widdle, by_variant in by_widdle.items():
                for variant, forms in by_variant.items():
                    ids = forms.get(which)
                    if not ids:
                        continue
                    label = " · ".join(p for p in (
                        campaign["name"], types.get(type_id, type_id), f"{widdle} next to Widdle",
                        variants.get(variant, variant), f"{which} formation",
                    ) if p)
                    yield CommunityFormation(
                        name=label,
                        source=source,
                        run_goal=entry["run_goal"],
                        campaign_name=campaign["name"],
                        positions=positions,
                        formation=tuple(int(i) for i in ids),
                        specializations={int(k): tuple(int(s) for s in v) for k, v in (forms.get("specs") or {}).items()},
                    )


def _object_literal(text: str, name: str):
    """The JSON object assigned to `const <name>=` in a JS file, without running the JS."""
    match = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*", text)
    if match is None:
        raise ValueError(f"{name} not found")
    value, _ = json.JSONDecoder().raw_decode(text, match.end())
    return value


def _campaign_key(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower())
    while words and words[0] in ("a", "the"):
        words = words[1:]
    return "".join(words)


def _normalised(points) -> list[tuple[float, float]]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    scale = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    return [((x - min(xs)) / scale, (y - min(ys)) / scale) for x, y in points]


def _same_shape(a, b) -> bool:
    if len(a) != len(b) or not a:
        return False
    return all(
        abs(p[0] - q[0]) <= _SHAPE_TOLERANCE and abs(p[1] - q[1]) <= _SHAPE_TOLERANCE
        for p, q in zip(_normalised(a), _normalised(b))
    )
