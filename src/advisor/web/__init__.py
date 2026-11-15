"""Web UI: display only, no domain logic."""

from __future__ import annotations

import time
from dataclasses import replace
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from advisor.account import Account, parse_account
from advisor.area_sets import merge as merge_area_sets
from advisor.catalogue import GEAR_SLOTS, ROLE_TAGS, Catalogue, parse_catalogue
from advisor.champion_models import ChampionModels
from advisor.explorer import RUN_GOALS, adventure_node, is_running, tree
from advisor.hints import hints as one_change_hints
from advisor.history import write_history
from advisor.next_steps import NEXT_STEP_GOALS, TOP_STEPS, FocusSets, Steps, measure_adventure, next_steps
from advisor.next_steps import ready_to_complete
from advisor.game_client import DEFAULT_LOG, HttpTransport, LiveGameClient, OfflineGameClient, Transport
from advisor.recommendation import recommend, with_search_problem
from advisor.recompute import ExecutorFactory, Recompute, process_pool
from advisor.refresh import Refresher
from advisor.request_log import RawResponses
from advisor.store import Store, load_config

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_TEMPLATES.env.filters["sci"] = lambda x: f"{x:,.0f}" if 100 <= x < 1e6 else f"{x:.3g}" if x < 100 else f"{x:.2e}".replace("e+", "e")

# Run Goals whose tab shows a Formation Recommendation.
_RECOMMENDED_GOALS = ("Gem Farm", "Gold Farm")
_RECOMMENDED_GOALS += ("Push",)  # with its Push Verdict (#32)


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
    live: showing the Store's cached data and refreshing it from the game servers.
    `recompute` holds the Formation Recommendations (precomputed after each Refresh in live
    mode); by default they are computed on demand when a tab is opened. With a `store`, the Area
    Set Cache there takes in every set the responses bring and serves all it holds."""
    raw: RawResponses = OfflineGameClient(log_path).fetch() if live is None else live.responses()
    catalogue, account = Catalogue(champions=()), Account(champions={})
    models = ChampionModels(catalogue, account)
    results = recompute or Recompute(_RECOMMENDED_GOALS)
    hints: dict[tuple, tuple] = {}  # "What would make this better", computed when a recommendation is opened
    focus = focus or FocusSets()  # Next Steps' Focus Sets (#35)
    steps = steps or Steps()  # each Focus adventure's measured one-changes

    def load(responses: RawResponses, *, refreshed: bool = True) -> None:
        """(Re)build everything the pages show from raw responses (at start and after a Refresh)."""
        nonlocal raw, catalogue, account, models
        if "getdefinitions" not in responses or "getuserdetails" not in responses:
            return  # live mode before the first Refresh: nothing to show yet
        raw = responses
        catalogue = parse_catalogue(raw)
        if store is not None:
            cache = merge_area_sets(store.load_area_sets(), catalogue.area_sets)
            store.save_area_sets(cache)
            catalogue = replace(catalogue, area_sets=cache)
        account = parse_account(raw)
        models = ChampionModels(catalogue, account)
        results.update(raw, catalogue, account, models, precompute=refreshed)
        hints.clear()
        steps.update(raw, account)
        steps.ensure((a, g) for g in NEXT_STEP_GOALS for a in focus.get(catalogue, account, g))

    load(raw, refreshed=False)
    app = FastAPI(
        title="Idle Champions Advisor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_refresh_on_start(live, results, steps) if live else None,
    )
    if live is None:
        app.state.header = lambda: {"mode": "offline"}
    else:
        live.on_refresh(load)
        _add_refresh(app, live, results)

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
        recommendation, stale = results.get(found, goal, patron) if goal in _RECOMMENDED_GOALS else (None, False)
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
            "focus_goals": [(g, found.id in focus.get(catalogue, account, g)) for g in NEXT_STEP_GOALS],
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
        found = catalogue.adventure(adventure_id)
        if found is None:
            raise HTTPException(404, "No such adventure")
        patron = patron if patron in found.patron_objectives else None
        rec, stale = results.get(found, goal, patron) if goal in _RECOMMENDED_GOALS else (None, False)
        key = (found.id, goal, patron)
        shown = hints.get(key, ())
        if rec is not None and key not in hints:
            rec = with_search_problem(catalogue, account, rec, models=models)  # persisted results carry none
            shown = one_change_hints(catalogue, account, rec, models=models)
            if not stale:  # a stale result is replaced once the recompute reaches it
                hints[key] = shown
        metric = rec.score_label.removesuffix(" (relative)") if rec else ""
        return _TEMPLATES.TemplateResponse(request, "_hints.html", {"hints": shown, "metric": metric})

    @app.post("/adventures/{adventure_id}/prove")
    def prove(adventure_id: int, goal: str = RUN_GOALS[0], patron: int | None = None) -> RedirectResponse:
        """"Prove it's the best" (#31): the exact solve (~60 s) replaces the cached recommendation;
        it keeps the fast formation unless it finds a better one."""
        found = catalogue.adventure(adventure_id)
        if found is None:
            raise HTTPException(404, "No such adventure")
        goal = goal if goal in RUN_GOALS else RUN_GOALS[0]
        patron = patron if patron in found.patron_objectives else None
        if goal in _RECOMMENDED_GOALS:
            proven = recommend(catalogue, account, found, goal, patron, models=models, solver="exact")
            results.put(found, goal, patron, proven)
            hints.pop((found.id, goal, patron), None)  # gains are measured against the shown formation
            if patron is None:
                steps.drop(found.id, goal)
        query = f"goal={quote(goal)}" + (f"&patron={patron}" if patron is not None else "")
        return RedirectResponse(f"/adventures/{found.id}?{query}", status_code=303)

    @app.post("/adventures/{adventure_id}/focus")
    def pin_focus(request: Request, adventure_id: int, goal: str, pin: bool = True) -> RedirectResponse:
        """Pin an adventure to a Run Goal's Focus Set, or unpin it (from the adventure header or
        the Next Steps tab)."""
        found = catalogue.adventure(adventure_id)
        if found is None or goal not in NEXT_STEP_GOALS:
            raise HTTPException(404, "No such adventure or Next Steps tab")
        focus.pin(catalogue, account, goal, found.id, pin)
        steps.ensure([(found.id, goal)] if pin else [])
        back = urlsplit(request.headers.get("referer") or f"/adventures/{found.id}")
        return RedirectResponse((back.path or "/") + (f"?{back.query}" if back.query else ""), status_code=303)

    @app.get("/next-steps", response_class=HTMLResponse)
    def next_steps_page(request: Request, goal: str = NEXT_STEP_GOALS[0], all: bool = False) -> HTMLResponse:
        goal = goal if goal in NEXT_STEP_GOALS else NEXT_STEP_GOALS[0]
        adventures = [a for i in focus.get(catalogue, account, goal) if (a := catalogue.adventure(i))]
        context = {
            "goal": goal,
            "goals": NEXT_STEP_GOALS,
            "all": all,
            "focus": [{"adventure": a, "running": is_running(catalogue, account, a)} for a in adventures],
        }
        return _TEMPLATES.TemplateResponse(request, "next_steps.html", context)

    @app.get("/next-steps/steps", response_class=HTMLResponse)
    def next_steps_list(request: Request, goal: str = NEXT_STEP_GOALS[0], all: bool = False) -> HTMLResponse:
        """The ranked steps, loaded by the Next Steps tab; measured in the background in live mode,
        on demand otherwise."""
        goal = goal if goal in NEXT_STEP_GOALS else NEXT_STEP_GOALS[0]
        adventures = [a for i in focus.get(catalogue, account, goal) if (a := catalogue.adventure(i))]
        steps.ensure((a.id, goal) for a in adventures)
        measured, waiting = {}, 0
        for adventure in adventures:
            found = steps.get(adventure.id, goal)
            if found is None and steps.pending(adventure.id, goal):
                waiting += 1
                continue
            if found is None:
                rec, stale = results.get(adventure, goal)
                found = measure_adventure(catalogue, account, rec, models=models)
                if not stale:
                    steps.put(adventure.id, goal, found)
            measured[adventure.id] = found
        ranked = () if waiting else next_steps(catalogue, account, adventures, measured)
        context = {
            "goal": goal,
            "all": all,
            "waiting": waiting,
            "focus_count": len(adventures),
            "steps": ranked if all else ranked[:TOP_STEPS],
            "total": len(ranked),
        }
        return _TEMPLATES.TemplateResponse(request, "_next_steps.html", context)

    @app.get("/next-steps/ready", response_class=HTMLResponse)
    def ready_list(request: Request) -> HTMLResponse:
        """Push's Ready to complete block, loaded by the Push tab (#36)."""

        def verdict_for(adventure, patron):
            return results.get(adventure, "Push", patron)[0].push_verdict

        ready = ready_to_complete(catalogue, account, verdict_for)
        return _TEMPLATES.TemplateResponse(request, "_ready.html", {"ready": ready})

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
    executor: ExecutorFactory = process_pool,
    steps_executor: ExecutorFactory = process_pool,
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
    return create_app(
        live=refresher,
        recompute=Recompute(_RECOMMENDED_GOALS, store=store, executor=executor),
        store=store,
        focus=FocusSets(store),
        steps=Steps(steps_executor),  # Next Steps' Focus adventures, measured in the background
    )


def _refresh_on_start(live: Refresher, results: Recompute, steps: Steps):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        live.start()
        yield
        results.close()
        steps.close()

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


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 120 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"
