"""Web UI: display only, no domain logic."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from advisor.account import parse_account
from advisor.catalogue import GEAR_SLOTS, ROLE_TAGS, parse_catalogue
from advisor.explorer import RUN_GOALS, adventure_node, tree
from advisor.game_client import OfflineGameClient

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


def create_app(log_path: Path) -> FastAPI:
    """The advisor in offline mode, reading responses from a request log (the game's or a fixture)."""
    raw = OfflineGameClient(log_path).fetch()
    catalogue = parse_catalogue(raw)
    account = parse_account(raw)

    app = FastAPI(title="Idle Champions Advisor", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/champions")

    @app.get("/champions", response_class=HTMLResponse)
    def champions(request: Request, role: str | None = None) -> HTMLResponse:
        role = role if role in ROLE_TAGS else None
        shown = [c for c in catalogue.champions if role is None or role in c.tags]
        rows = [{"champion": c, "owned": account.champions.get(c.id)} for c in shown]
        template = "_champion_rows.html" if request.headers.get("HX-Request") else "champions.html"
        return _TEMPLATES.TemplateResponse(
            request, template, {"rows": rows, "role": role, "role_tags": ROLE_TAGS}
        )

    @app.get("/adventures")
    def adventures() -> RedirectResponse:
        first = tree(catalogue, account)[0].adventures[0].adventure
        return RedirectResponse(f"/adventures/{first.id}")

    @app.get("/adventures/{adventure_id}", response_class=HTMLResponse)
    def adventure(
        request: Request, adventure_id: int, goal: str = RUN_GOALS[0], patron: int | None = None
    ) -> HTMLResponse:
        found = catalogue.adventure(adventure_id)
        if found is None:
            raise HTTPException(404, "No such adventure")
        goal = goal if goal in RUN_GOALS else RUN_GOALS[0]
        patron = patron if patron in found.patron_objectives else None
        context = {
            "tree": tree(catalogue, account),
            "node": adventure_node(catalogue, account, found),
            "campaign": catalogue.campaign(found.campaign_id),
            "base": catalogue.adventure(found.base_id) if found.base_id else None,
            "goal": goal,
            "goals": RUN_GOALS,
            "patron": patron,
            "patrons": [
                {"patron": catalogue.patron(pid), "area": area, "locked": pid not in account.unlocked_patron_ids}
                for pid, area in found.patron_objectives.items()
            ],
        }
        return _TEMPLATES.TemplateResponse(request, "formation.html", context)
    @app.get("/champions/{champion_id}", response_class=HTMLResponse)
    def champion_detail(request: Request, champion_id: int) -> HTMLResponse:
        champion = catalogue.find(champion_id)
        if champion is None:
            raise HTTPException(status_code=404, detail="No such champion")
        owned = account.champions.get(champion_id)
        gear = []
        for slot in GEAR_SLOTS:
            held = owned.gear.get(slot) if owned else None
            item = catalogue.gear_item(champion_id, slot, held.rarity) if held else None
            gear.append({"slot": slot, "owned": held, "item": item})
        owned_feats = owned.feats if owned else ()
        feats = [
            {"id": f, "feat": catalogue.feats.get(f), "owned": True, "equipped": f in owned.equipped_feats}
            for f in owned_feats
        ] + [
            {"id": f.id, "feat": f, "owned": False, "equipped": False}
            for f in catalogue.feats_of(champion_id)
            if f.id not in owned_feats
        ]
        return _TEMPLATES.TemplateResponse(
            request,
            "champion_detail.html",
            {
                "champion": champion,
                "owned": owned,
                "gear": gear,
                "feats": feats,
                "spec_choices": catalogue.specializations.get(champion_id, ()),
            },
        )

    return app
