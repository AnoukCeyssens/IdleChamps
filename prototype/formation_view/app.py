# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "jinja2"]
# ///
"""PROTOTYPE, throwaway. Answers: "What should the per-adventure formation view look like?"

Three structurally different variants of the formation view on one route,
switchable via ?variant=A|B|C (or the floating bar / arrow keys):

  A  Explorer  : adventure tree sidebar -> one adventure -> Run Goal tabs -> grid + side panels
  B  Matrix    : every adventure x every Run Goal in one table; click a cell to expand it inline (htmx)
  C  Compare   : one adventure, all four Run Goals side by side, with differences highlighted

Run:  uv run prototype/formation_view/app.py   ->  http://127.0.0.1:8765/prototype/formation-view

Data: REAL champion/adventure definitions and owned champions, parsed in memory from the
game's webRequestLog.txt response bodies only (request lines, which carry credentials, are
never parsed; credential fields are dropped; nothing is written to disk).
Recommendations are STUBS (greedy pick by tag and level), NOT the real search. Scores,
gaps, Scoring Assumptions and hints are placeholders to judge layout, not numbers.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

LOG = Path.home() / (
    "Library/Application Support/Steam/steamapps/common/IdleChampions/IdleDragonsMac.app/"
    "Contents/Resources/Data/StreamingAssets/downloaded_files/webRequestLog.txt"
)
GOALS = ["Push", "Complete", "Gem Farm", "Gold Farm"]
VARIANTS = {"A": "Explorer", "B": "Matrix", "C": "Compare"}
PATRONS = {"1": "Mirt", "2": "Vajra", "3": "Strahd", "4": "Zariel", "5": "Elminster"}
GOAL_TAGS = {
    "Push": ["dps", "support", "debuff", "control"],
    "Complete": ["dps", "support", "healing", "tanking"],
    "Gem Farm": ["speed", "dps", "support"],
    "Gold Farm": ["gold", "dps", "support"],
}


# ---------------------------------------------------------------- data (in memory only)
def load():
    blocks = LOG.read_text(encoding="utf-8").split("*" * 50)
    resp = {}
    for b in blocks:
        parts = [p for p in b.strip().split("\n") if p.strip()]
        if len(parts) < 2:
            continue
        m = re.search(r"call=(\w+)", parts[0])  # only the call name is read from the URL
        if m:
            resp[m.group(1).lower()] = json.loads(parts[1])
    hero_defs = {h["id"]: h for h in resp["getdefinitions"]["hero_defines"]}
    ud = resp["getuserdetails"]
    D, det = ud["defines"], ud["details"]
    del resp, ud
    spec_names = {u["id"]: u["specialization_name"] for u in D["upgrade_defines"] if u.get("specialization_name")}
    owned = []
    for h in det["heroes"]:
        if str(h.get("owned")) != "1":
            continue
        hd = hero_defs.get(int(h["hero_id"]))
        if not hd:
            continue
        owned.append({
            "id": hd["id"], "name": hd["name"], "seat": hd["seat_id"], "tags": hd["tags"],
            "level": int(h["level"]),
            "specs": [spec_names.get(s, f"Spec #{s}") for s in h.get("specialization_choices", [])],
            "feats": [f"Feat #{f}" for f in h.get("active_feats", [])],
            "feat_slots": h.get("feat_slots", 0),
        })
    owned_ids = {o["id"] for o in owned}
    unowned = [h for h in hero_defs.values() if h["id"] not in owned_ids and h.get("is_available", True)]
    stats = det["stats"]
    camps = {c["id"]: c for c in D["campaign_defines"]}
    advs = {}
    for a in D["adventure_defines"]:
        c = camps.get(a["campaign_id"])
        layout = next((g["formation"] for g in a["game_changes"] if g["type"] == "formation"), None)
        if layout is None and c:
            layout = next((g["formation"] for g in c["game_changes"] if g["type"] == "formation"), [])
        blocked = set()
        allow = disallow = None
        forced = []
        for g in a["game_changes"]:
            t = g["type"]
            if t == "slot_escort":
                blocked.update([g["slot_id"]] if "slot_id" in g else g.get("slot_ids", []))
            elif t == "slot_escort_by_area":
                for e in g["escorts"]:
                    blocked.update(e["slot_ids"])
            elif t == "only_allow_crusaders":
                allow = g.get("by_expr", {}).get("expr") or ("ids:" + ",".join(map(str, g.get("by_ids", []))))
            elif t == "disallow_crusaders":
                disallow = g.get("by_expr", {}).get("expr") or ("ids:" + ",".join(map(str, g.get("by_ids", []))))
            elif t == "force_use_heroes":
                forced += g["hero_ids"]
        obj = next((int(o["area"]) for o in a["objectives"] if o["condition"] == "complete_area"), None)
        best = int(stats.get(f"highest_area_completed_ever_c{a['id']}", 0))
        advs[a["id"]] = {
            "id": a["id"], "name": a["name"], "campaign_id": a["campaign_id"],
            "campaign": c["name"] if c else f"Campaign {a['campaign_id']}",
            "is_event": bool(c and c["properties"].get("is_event")),
            "parent": a.get("variant_adventure_id") or None,
            "objective": obj, "best": best, "completed": bool(obj and best >= obj),
            "restrictions": a["restrictions_text"], "layout": layout or [], "blocked": blocked,
            "allow": allow, "disallow": disallow, "forced": forced,
            "patrons": {pid: int(v["1"][0]["area"]) for pid, v in (a["patron_objectives"] or {}).items()},
            "variants": [],
        }
    for a in advs.values():
        if a["parent"] in advs:
            advs[a["parent"]]["variants"].append(a)
    tree = []
    for cid, c in camps.items():
        bases = [a for a in advs.values() if a["campaign_id"] == cid and a["parent"] not in advs]
        tree.append({"id": cid, "name": c["name"], "is_event": bool(c["properties"].get("is_event")),
                     "adventures": sorted(bases, key=lambda a: a["id"])})
    tree.sort(key=lambda c: (c["is_event"], c["name"]))
    familiars = len(det["familiars"])
    cores = [{"id": k, "area_goal": v.get("area_goal")} for k, v in det["modron_saves"].items()]
    patrons_unlocked = {str(p["patron_id"]): p["unlocked"] for p in det["patrons"]}
    return owned, unowned, advs, tree, familiars, cores, patrons_unlocked


OWNED, UNOWNED, ADVS, TREE, FAMILIARS, CORES, PATRONS_UNLOCKED = load()


# ---------------------------------------------------------------- STUB recommendation
def _expr_ok(expr: str | None, hero: dict) -> bool:
    if expr.startswith("ids:"):
        return str(hero["id"]) in expr[4:].split(",")
    py = re.sub(r"HasTag\(`(\w+)`\)", lambda m: str(m.group(1) in hero["tags"]), expr)
    py = py.replace("||", " or ").replace("&&", " and ").replace("!", " not ")
    py = re.sub(r"\b(?!True\b|False\b|and\b|or\b|not\b)[a-z_]+\b", "False", py)
    try:
        return bool(eval(py, {"__builtins__": {}}))  # prototype only; input is game definitions
    except Exception:
        return True


def _h(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def recommend(adv_id: int, goal: str, patron: str | None = None):
    a = ADVS[adv_id]
    excluded = []
    pool = []
    for h in OWNED:
        if a["allow"] and not _expr_ok(a["allow"], h):
            excluded.append((h["name"], "not allowed by restriction"))
        elif a["disallow"] and _expr_ok(a["disallow"], h):
            excluded.append((h["name"], "disallowed by restriction"))
        else:
            pool.append(h)
    tags = GOAL_TAGS[goal]

    def key(h):
        rank = next((i for i, t in enumerate(tags) if t in h["tags"]), len(tags))
        return (h["id"] not in a["forced"], rank, -h["level"])

    pool.sort(key=key)
    slots = [i for i in range(len(a["layout"])) if i not in a["blocked"]]
    picked, seats = [], set()
    for h in pool:
        if len(picked) == len(slots):
            break
        if h["seat"] in seats:
            excluded.append((h["name"], f"seat {h['seat']} taken by a better pick"))
            continue
        seats.add(h["seat"])
        picked.append(h)
    # main DPS to the most connected open slot, the rest by adjacency to it
    by_adj = sorted(slots, key=lambda i: -len(a["layout"][i]["adj"]))
    grid = [None] * len(a["layout"])
    if picked:
        dps_slot = by_adj[0]
        grid[dps_slot] = picked[0]
        rest = sorted(slots[:], key=lambda i: (i not in a["layout"][dps_slot]["adj"], -len(a["layout"][i]["adj"])))
        for i, h in zip([s for s in rest if s != dps_slot], picked[1:]):
            grid[i] = h
    cells = []
    for i, s in enumerate(a["layout"]):
        h = grid[i]
        role = None
        if h:
            role = "main DPS" if h is picked[0] else next((t for t in tags if t in h["tags"]), "filler")
        cells.append({"i": i, "x": s["x"], "y": s["y"], "col": s["col"], "adj": s["adj"],
                      "blocked": i in a["blocked"], "hero": h, "role": role})
    seed = _h(adv_id, goal, patron)
    mant, exp = 1 + seed % 900 / 100, 8 + sum(h["level"] for h in picked) // 400
    score = f"{mant:.2f}e{exp}"
    gap = f"{(seed % 47) / 10:.1f}%"
    assumptions = {
        "Push": ["All formation stacks are at their cap by the wall area", "Random effects at expected value"],
        "Complete": ["Random effects at expected value", "Survives to objective area at current gear"],
        "Gem Farm": ["Haste stacks rebuilt each reset (assumed 100% of cap)", "Modron reset at the stated area"],
        "Gold Farm": ["You click actively ~ the familiar-click equivalent", "Daily-cap gold effects collected"],
    }[goal]
    unowned_fit = [h for h in UNOWNED if tags[0] in h["tags"]]
    hint_hero = unowned_fit[seed % len(unowned_fit)]["name"] if unowned_fit else None
    weakest = min(picked, key=lambda h: h["level"]) if picked else None
    hints = []
    if weakest:
        hints.append({"text": f"Level {weakest['name']} from {weakest['level']} to {weakest['level'] + 100}", "gain": f"+{5 + seed % 20}%"})
    if hint_hero:
        hints.append({"text": f"Get {hint_hero} ({tags[0]})", "gain": f"+{20 + seed % 60}%"})
    if picked:
        hints.append({"text": f"Unlock a 2nd feat slot on {picked[0]['name']}", "gain": f"+{3 + seed % 9}%"})
    if patron and not PATRONS_UNLOCKED.get(patron):
        hints.insert(0, {"text": f"Unlock patron {PATRONS[patron]} first (locked)", "gain": "required"})
    farm = goal in ("Gem Farm", "Gold Farm")
    fam = None
    if farm and FAMILIARS:
        fam_slots = [c["i"] for c in cells if c["hero"]][: FAMILIARS]
        fam = {"count": FAMILIARS, "seats": fam_slots, "clicks": max(0, FAMILIARS - len(fam_slots))}
    modron = None
    if goal == "Gem Farm" and CORES:
        reset = max(50, int((a["best"] or 100) * 0.9) // 5 * 5)
        modron = {"core": CORES[0]["id"], "reset_area": reset, "current_goal": CORES[0]["area_goal"]}
    return {"adv": a, "goal": goal, "patron": patron, "cells": cells, "picked": picked,
            "score": score, "gap": gap, "assumptions": assumptions, "hints": hints,
            "excluded": excluded[:8], "familiars": fam, "modron": modron}


# ---------------------------------------------------------------- web
app = FastAPI()
T = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/")
def root():
    return RedirectResponse("/prototype/formation-view")


def _common(request: Request, variant: str, adv: int | None, goal: str, patron: str | None):
    adv = adv if adv in ADVS else TREE[0]["adventures"][0]["id"]
    goal = goal if goal in GOALS else "Push"
    patron = patron if patron in ADVS[adv]["patrons"] else None
    return {"request": request, "variant": variant, "variants": VARIANTS, "goals": GOALS,
            "tree": TREE, "advs": ADVS, "adv": ADVS[adv], "goal": goal, "patron": patron,
            "patron_names": PATRONS, "patrons_unlocked": PATRONS_UNLOCKED, "recommend": recommend,
            "owned_count": len(OWNED)}


@app.get("/prototype/formation-view", response_class=HTMLResponse)
def view(request: Request, variant: str = "A", adv: int | None = None, goal: str = "Push",
         patron: str | None = None, q: str = "", campaign: str = "", hide_done: bool = False):
    variant = variant if variant in VARIANTS else "A"
    ctx = _common(request, variant, adv, goal, patron)
    ctx.update(q=q, campaign=int(campaign) if campaign.isdigit() else None, hide_done=hide_done)
    return T.TemplateResponse(request, f"variant_{variant.lower()}.html", ctx)


@app.get("/prototype/formation-view/cell", response_class=HTMLResponse)
def cell(request: Request, adv: int, goal: str, patron: str | None = None):
    ctx = _common(request, "B", adv, goal, patron)
    ctx["r"] = recommend(ctx["adv"]["id"], ctx["goal"], ctx["patron"])
    return T.TemplateResponse(request, "_b_detail.html", ctx)


if __name__ == "__main__":
    print(f"PROTOTYPE: {len(OWNED)} owned champions, {len(ADVS)} adventures loaded (in memory only)")
    print("Open http://127.0.0.1:8765/prototype/formation-view")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
