"""Web UI: display only, no domain logic."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from advisor.account import Account, parse_account
from advisor.catalogue import GEAR_SLOTS, ROLE_TAGS, Catalogue, parse_catalogue
from advisor.champion_models import ChampionModels
from advisor.explorer import RUN_GOALS, adventure_node, tree
from advisor.history import write_history
from advisor.game_client import DEFAULT_LOG, HttpTransport, LiveGameClient, OfflineGameClient, Transport
from advisor.recommendation import recommend
from advisor.refresh import Refresher
from advisor.request_log import RawResponses
from advisor.store import Store, load_config

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_TEMPLATES.env.filters["sci"] = lambda x: f"{x:,.0f}" if 100 <= x < 1e6 else f"{x:.3g}" if x < 100 else f"{x:.2e}".replace("e+", "e")

# Run Goals whose tab shows a Formation Recommendation so far (Push: #32).
_RECOMMENDED_GOALS = ("Gem Farm", "Gold Farm")


def create_app(log_path: Path | None = None, *, live: Refresher | None = None) -> FastAPI:
    """The advisor, either in offline mode on a request log (the game's or a fixture), or
    live: showing the Store's cached data and refreshing it from the game servers."""
    raw: RawResponses = OfflineGameClient(log_path).fetch() if live is None else live.responses()
    catalogue, account = Catalogue(champions=()), Account(champions={})
    models = ChampionModels(catalogue, account)
    recommendations: dict[tuple, object] = {}  # computed on demand when a tab is opened

    def load(responses: RawResponses) -> None:
        """(Re)build everything the pages show from raw responses (at start and after a Refresh)."""
        nonlocal raw, catalogue, account, models
        if "getdefinitions" not in responses or "getuserdetails" not in responses:
            return  # live mode before the first Refresh: nothing to show yet
        raw = responses
        catalogue = parse_catalogue(raw)
        account = parse_account(raw)
        models = ChampionModels(catalogue, account)
        recommendations.clear()

    load(raw)
    app = FastAPI(
        title="Idle Champions Advisor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_refresh_on_start(live) if live else None,
    )
    if live is None:
        app.state.header = lambda: {"mode": "offline"}
    else:
        live.on_refresh(load)
        _add_refresh(app, live)

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
        campaigns = tree(catalogue, account)
        if not campaigns:  # live mode before the first Refresh
            return RedirectResponse("/champions")
        first = campaigns[0].adventures[0].adventure
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
            "recommendation": _recommendation(found, goal, patron) if goal in _RECOMMENDED_GOALS else None,
            "patrons": [
                {"patron": catalogue.patron(pid), "area": area, "locked": pid not in account.unlocked_patron_ids}
                for pid, area in found.patron_objectives.items()
            ],
        }
        return _TEMPLATES.TemplateResponse(request, "formation.html", context)

    def _recommendation(found, goal, patron):
        key = (found.id, goal, patron)
        if key not in recommendations:
            recommendations[key] = recommend(catalogue, account, found, goal, patron, models=models)
        return recommendations[key]

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


def create_live_app(
    data_dir: Path,
    *,
    transport: Transport | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    background: Callable | None = None,
) -> FastAPI:
    """The advisor in live mode on an app data folder (config, caches)."""
    config = load_config(data_dir, DEFAULT_LOG)
    client = LiveGameClient(
        transport or HttpTransport(), config.request_log, clock=clock or time.monotonic, sleep=sleep
    )
    options = {"background": background} if background else {}
    store = Store(data_dir)
    refresher = Refresher(client, store, cooldown=config.cooldown_seconds, clock=clock or time.time, **options)
    refresher.on_refresh(lambda responses: write_history(store, responses))
    return create_app(live=refresher)


def _refresh_on_start(live: Refresher):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        live.start()
        yield

    return lifespan


def _add_refresh(app: FastAPI, live: Refresher) -> None:
    """The header's Refresh button, and the header itself (polled to stay current)."""

    def header() -> dict:
        status = live.status()
        ago = None
        if status.refreshed_at is not None:
            ago = _ago(live.now() - status.refreshed_at)
        return {"mode": "live", "status": status, "ago": ago}

    app.state.header = header

    @app.post("/refresh")
    def refresh(request: Request) -> RedirectResponse:
        live.refresh()  # ignored while one runs or during the cooldown
        back = urlsplit(request.headers.get("referer") or "/")
        return RedirectResponse((back.path or "/") + (f"?{back.query}" if back.query else ""), status_code=303)

    @app.get("/header", response_class=HTMLResponse)
    def header_partial(request: Request, seen: int = -1) -> HTMLResponse:
        response = _TEMPLATES.TemplateResponse(request, "_header.html", {})
        status = live.status()
        if seen != status.generation and not status.running:
            response.headers["HX-Refresh"] = "true"  # new data arrived: reload the page around it
        return response


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 120 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"
