"""Web UI: display only, no domain logic."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from advisor.background import ExecutorFactory, Workers, process_pool
from advisor.catalogue import GEAR_SLOTS, ROLE_TAGS
from advisor.champion_models import ModelStatus
from advisor.explorer import RUN_GOALS, adventure_node, is_running, tree
from advisor.history import write_history
from advisor.next_steps import NEXT_STEP_GOALS, TOP_STEPS, FocusSets, Steps
from advisor.game_client import DEFAULT_LOG, HttpTransport, LiveGameClient, OfflineGameClient, Transport
from advisor.recompute import Recompute
from advisor.refresh import Refresher
from advisor.session import RECOMMENDED_GOALS, Session
from advisor.store import Store, load_config

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_TEMPLATES.env.filters["sci"] = lambda x: f"{x:,.0f}" if 100 <= x < 1e6 else f"{x:.3g}" if x < 100 else f"{x:.2e}".replace("e+", "e")


def create_app(
    log_path: Path | None = None,
    *,
    live: Refresher | None = None,
    recompute: Recompute | None = None,
    focus: FocusSets | None = None,
    steps: Steps | None = None,
    store: Store | None = None,
) -> FastAPI:
    """The advisor, either in offline mode on a request log (the game's or a fixture), or
    live: showing the Store's cached data and refreshing it from the game servers. What the
    pages show comes from a `Session` (`advisor.session`); this module only renders it."""
    session = Session(results=recompute, focus=focus, steps=steps, store=store)
    session.load(OfflineGameClient(log_path).fetch() if live is None else live.responses(), refreshed=False)
    app = FastAPI(
        title="Idle Champions Advisor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_refresh_on_start(live, session) if live else None,
    )
    if live is None:
        app.state.header = lambda: {"mode": "offline"}
    else:
        live.on_refresh(session.load)
        _add_refresh(app, live, session.results)

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/champions")

    @app.get("/champions", response_class=HTMLResponse)
    def champions(request: Request, role: str | None = None, status: str | None = None) -> HTMLResponse:
        role = role if role in ROLE_TAGS else None
        status = status if status in _STATUS_NAMES else None
        models = session.models
        shown = [
            c for c in session.catalogue.champions
            if (role is None or role in c.tags) and (status is None or models.status(c.id).value == status)
        ]
        rows = [{"champion": c, "owned": session.account.champions.get(c.id), "status": models.status(c.id)} for c in shown]
        template = "_champion_rows.html" if request.headers.get("HX-Request") else "champions.html"
        return _TEMPLATES.TemplateResponse(
            request, template,
            {"rows": rows, "role": role, "role_tags": ROLE_TAGS, "status": status, "statuses": _STATUS_NAMES},
        )

    @app.get("/model-status", response_class=HTMLResponse)
    def model_status_page(request: Request) -> HTMLResponse:
        overview = session.model_status_overview()
        context = {
            "table": overview.table,
            "flags": overview.flags,
            "calibration": overview.calibration,
            "checks": overview.checks,
            "coverage": overview.coverage,
            "history": overview.history,
            "data_folder": str(store.folder) if store is not None else None,
            "opened": request.query_params.get("opened") == "1",
        }
        return _TEMPLATES.TemplateResponse(request, "model_status.html", context)

    if store is not None:

        @app.post("/data-folder")
        def data_folder() -> RedirectResponse:
            """Open the app data folder in the system's file browser."""
            store.folder.mkdir(parents=True, exist_ok=True)
            open_folder(store.folder)
            return RedirectResponse("/model-status?opened=1", status_code=303)

    def _adventure(adventure_id: int):
        found = session.catalogue.adventure(adventure_id)
        if found is None:
            raise HTTPException(404, "No such adventure")
        return found

    @app.get("/adventures")
    def adventures() -> RedirectResponse:
        campaigns = tree(session.catalogue, session.account)
        if not campaigns:  # live mode before the first Refresh
            return RedirectResponse("/champions")
        first = campaigns[0].adventures[0].adventure
        return RedirectResponse(f"/adventures/{first.id}")

    @app.get("/adventures/{adventure_id}", response_class=HTMLResponse)
    def adventure(
        request: Request, adventure_id: int, goal: str = RUN_GOALS[0], patron: int | None = None
    ) -> HTMLResponse:
        found = _adventure(adventure_id)
        goal = goal if goal in RUN_GOALS else RUN_GOALS[0]
        patron = patron if patron in found.patron_objectives else None
        catalogue, account = session.catalogue, session.account
        recommendation, stale = session.recommendation(found, goal, patron)
        context = {
            "tree": tree(catalogue, account),
            "node": adventure_node(catalogue, account, found),
            "campaign": catalogue.campaign(found.campaign_id),
            "base": catalogue.adventure(found.base_id) if found.base_id else None,
            "goal": goal,
            "goals": RUN_GOALS,
            "patron": patron,
            "recommendation": recommendation,
            "stale": stale,  # from before the running recompute
            "focus_goals": [(g, session.is_focus(found, g)) for g in NEXT_STEP_GOALS],
            "patrons": [
                {"patron": catalogue.patron(pid), "area": area, "locked": pid not in account.unlocked_patron_ids}
                for pid, area in found.patron_objectives.items()
            ],
        }
        return _TEMPLATES.TemplateResponse(request, "formation.html", context)

    @app.get("/adventures/{adventure_id}/hints", response_class=HTMLResponse)
    def adventure_hints(
        request: Request, adventure_id: int, goal: str = RUN_GOALS[0], patron: int | None = None
    ) -> HTMLResponse:
        """The right rail's hints, loaded by the Formation view once the recommendation is shown."""
        found = _adventure(adventure_id)
        patron = patron if patron in found.patron_objectives else None
        shown, metric = session.rail_hints(found, goal, patron)
        return _TEMPLATES.TemplateResponse(request, "_hints.html", {"hints": shown, "metric": metric})

    @app.post("/adventures/{adventure_id}/prove")
    def prove(adventure_id: int, goal: str = RUN_GOALS[0], patron: int | None = None) -> RedirectResponse:
        """"Prove it's the best" (#31)."""
        found = _adventure(adventure_id)
        goal = goal if goal in RUN_GOALS else RUN_GOALS[0]
        patron = patron if patron in found.patron_objectives else None
        session.prove(found, goal, patron)
        query = f"goal={quote(goal)}" + (f"&patron={patron}" if patron is not None else "")
        return RedirectResponse(f"/adventures/{found.id}?{query}", status_code=303)

    @app.post("/adventures/{adventure_id}/focus")
    def pin_focus(request: Request, adventure_id: int, goal: str, pin: bool = True) -> RedirectResponse:
        """Pin an adventure to a Run Goal's Focus Set, or unpin it (from the adventure header or
        the Next Steps tab)."""
        found = session.catalogue.adventure(adventure_id)
        if found is None or goal not in NEXT_STEP_GOALS:
            raise HTTPException(404, "No such adventure or Next Steps tab")
        session.pin(found, goal, pin)
        back = urlsplit(request.headers.get("referer") or f"/adventures/{found.id}")
        return RedirectResponse((back.path or "/") + (f"?{back.query}" if back.query else ""), status_code=303)

    @app.get("/next-steps", response_class=HTMLResponse)
    def next_steps_page(request: Request, goal: str = NEXT_STEP_GOALS[0], all: bool = False) -> HTMLResponse:
        goal = goal if goal in NEXT_STEP_GOALS else NEXT_STEP_GOALS[0]
        context = {
            "goal": goal,
            "goals": NEXT_STEP_GOALS,
            "all": all,
            "focus": [
                {"adventure": a, "running": is_running(session.catalogue, session.account, a)}
                for a in session.focus_adventures(goal)
            ],
        }
        return _TEMPLATES.TemplateResponse(request, "next_steps.html", context)

    @app.get("/next-steps/steps", response_class=HTMLResponse)
    def next_steps_list(request: Request, goal: str = NEXT_STEP_GOALS[0], all: bool = False) -> HTMLResponse:
        """The ranked steps, loaded by the Next Steps tab; measured in the background in live mode,
        on demand otherwise."""
        goal = goal if goal in NEXT_STEP_GOALS else NEXT_STEP_GOALS[0]
        ranked = session.ranked_steps(goal)
        context = {
            "goal": goal,
            "all": all,
            "waiting": ranked.waiting,
            "focus_count": ranked.focus_count,
            "steps": ranked.steps if all else ranked.steps[:TOP_STEPS],
            "total": len(ranked.steps),
        }
        return _TEMPLATES.TemplateResponse(request, "_next_steps.html", context)

    @app.get("/next-steps/ready", response_class=HTMLResponse)
    def ready_list(request: Request) -> HTMLResponse:
        """Push's Ready to complete block, loaded by the Push tab (#36)."""
        return _TEMPLATES.TemplateResponse(request, "_ready.html", {"ready": session.ready()})

    @app.get("/champions/{champion_id}", response_class=HTMLResponse)
    def champion_detail(request: Request, champion_id: int) -> HTMLResponse:
        catalogue = session.catalogue
        champion = catalogue.find(champion_id)
        if champion is None:
            raise HTTPException(status_code=404, detail="No such champion")
        owned = session.account.champions.get(champion_id)
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
                "model_status": session.models.status(champion_id),
                "model_evidence": session.evidence(champion_id),
                "step_goals": NEXT_STEP_GOALS,
                **session.champion_usage(champion_id),  # how the advisor uses it (#37)
            },
        )

    @app.get("/champions/{champion_id}/next-steps", response_class=HTMLResponse)
    def champion_next_steps(request: Request, champion_id: int, goal: str = NEXT_STEP_GOALS[0]) -> HTMLResponse:
        """One Run Goal's Next Steps that involve this champion, loaded by its page."""
        champion = session.catalogue.find(champion_id)
        if champion is None or goal not in NEXT_STEP_GOALS:
            raise HTTPException(404, "No such champion or Next Steps tab")
        steps, waiting = session.champion_steps(champion_id, goal)
        context = {"champion": champion, "goal": goal, "waiting": waiting, "steps": steps}
        return _TEMPLATES.TemplateResponse(request, "_champion_steps.html", context)

    return app


def create_live_app(
    data_dir: Path,
    *,
    transport: Transport | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    background: Callable | None = None,
    executor: ExecutorFactory = process_pool,
) -> FastAPI:
    """The advisor in live mode on an app data folder (config, caches). `executor` makes the one
    background pool the Recompute and Next Steps share."""
    config = load_config(data_dir, DEFAULT_LOG)
    client = LiveGameClient(
        transport or HttpTransport(), config.request_log, clock=clock or time.monotonic, sleep=sleep
    )
    options = {"background": background} if background else {}
    store = Store(data_dir)
    refresher = Refresher(client, store, cooldown=config.cooldown_seconds, clock=clock or time.time, **options)
    refresher.on_refresh(lambda responses: write_history(store, responses))
    workers = Workers(executor)
    return create_app(
        live=refresher,
        recompute=Recompute(RECOMMENDED_GOALS, store=store, workers=workers),
        store=store,
        focus=FocusSets(store),
        steps=Steps(workers),  # Next Steps' Focus adventures, measured in the same background pool
    )


def _refresh_on_start(live: Refresher, session: Session):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        live.start()
        yield
        session.results.close()
        session.steps.close()

    return lifespan


def _add_refresh(app: FastAPI, live: Refresher, results: Recompute) -> None:
    """The header's Refresh button, and the header itself (polled to stay current)."""

    def header() -> dict:
        status = live.status()
        ago = None
        if status.refreshed_at is not None:
            ago = _ago(live.now() - status.refreshed_at)
        return {"mode": "live", "status": status, "ago": ago, "recompute": results.progress()}

    app.state.header = header

    @app.post("/refresh")
    def refresh(request: Request) -> RedirectResponse:
        live.refresh()  # ignored while one runs or during the cooldown
        back = urlsplit(request.headers.get("referer") or "/")
        return RedirectResponse((back.path or "/") + (f"?{back.query}" if back.query else ""), status_code=303)

    @app.get("/header", response_class=HTMLResponse)
    def header_partial(request: Request, seen: int = -1, computed: int | None = None) -> HTMLResponse:
        response = _TEMPLATES.TemplateResponse(request, "_header.html", {})
        status, progress = live.status(), results.progress()
        if seen != status.generation and not status.running:
            response.headers["HX-Refresh"] = "true"  # new data arrived: reload the page around it
        if computed is not None and computed != progress.completed and not progress.running:
            response.headers["HX-Refresh"] = "true"  # a recompute finished: swap stale results for fresh
        return response


_STATUS_NAMES = tuple(s.value for s in ModelStatus)


def open_folder(folder: Path) -> None:
    """Show a folder in the system's file browser (Finder on macOS)."""
    opener = "open" if sys.platform == "darwin" else "explorer" if sys.platform == "win32" else "xdg-open"
    subprocess.Popen([opener, str(folder)])


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 120 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"
