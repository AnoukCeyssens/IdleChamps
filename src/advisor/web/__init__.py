"""Web UI: display only, no domain logic."""

from __future__ import annotations

import subprocess
import sys
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
from advisor import model_status
from advisor.champion_models import ChampionModels, ModelStatus
from advisor.explorer import RUN_GOALS, adventure_node, tree
from advisor.hints import hints as one_change_hints
from advisor.history import write_history
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
    log_path: Path | None = None, *, live: Refresher | None = None, recompute: Recompute | None = None,
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
    ledger = model_status.from_json(store.load_model_status()) if store is not None else model_status.Ledger()
    checks: tuple[model_status.DamageCheck, ...] = ()  # the live damage checks on the shown snapshot

    def load(responses: RawResponses, *, refreshed: bool = True) -> None:
        """(Re)build everything the pages show from raw responses (at start and after a Refresh)."""
        nonlocal raw, catalogue, account, models, ledger, checks
        if "getdefinitions" not in responses or "getuserdetails" not in responses:
            return  # live mode before the first Refresh: nothing to show yet
        raw = responses
        catalogue = parse_catalogue(raw)
        if store is not None:
            cache = merge_area_sets(store.load_area_sets(), catalogue.area_sets)
            store.save_area_sets(cache)
            catalogue = replace(catalogue, area_sets=cache)
        account = parse_account(raw)
        checks = model_status.damage_checks(catalogue, account, ChampionModels(catalogue, account))
        ledger = model_status.advance(ledger, catalogue, account, checks)
        if store is not None:
            try:
                store.save_model_status(model_status.to_json(ledger))
            except OSError:
                pass  # kept in memory; the next Refresh saves it
        models = ChampionModels(catalogue, account, statuses=ledger.statuses())
        results.update(raw, catalogue, account, models, precompute=refreshed)
        hints.clear()

    load(raw, refreshed=False)
    app = FastAPI(
        title="Idle Champions Advisor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_refresh_on_start(live, results) if live else None,
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
    def champions(request: Request, role: str | None = None, status: str | None = None) -> HTMLResponse:
        role = role if role in ROLE_TAGS else None
        status = status if status in _STATUS_NAMES else None
        shown = [
            c for c in catalogue.champions
            if (role is None or role in c.tags) and (status is None or models.status(c.id).value == status)
        ]
        rows = [{"champion": c, "owned": account.champions.get(c.id), "status": models.status(c.id)} for c in shown]
        template = "_champion_rows.html" if request.headers.get("HX-Request") else "champions.html"
        return _TEMPLATES.TemplateResponse(
            request, template,
            {"rows": rows, "role": role, "role_tags": ROLE_TAGS, "status": status, "statuses": _STATUS_NAMES},
        )

    @app.get("/model-status", response_class=HTMLResponse)
    def model_status_page(request: Request) -> HTMLResponse:
        table = [{"champion": c, "status": models.status(c.id)} for c in catalogue.champions]
        with_sets = [a for a in catalogue.adventures if a.area_set_id is not None]
        known = [a for a in with_sets if a.area_set_id in catalogue.area_sets]
        coverage = (
            f"Monster placement known for {len(known)} of {len(with_sets)} adventures "
            f"({len({a.area_set_id for a in known})} of {len({a.area_set_id for a in with_sets})} area sets)"
        )
        if store is None:
            history = "History: not recorded in offline mode"
        else:
            kept = store.load_history()
            first = next((r.get("server_time") for r in kept if r.get("server_time")), None)
            history = f"History: {len(kept)} record{'' if len(kept) == 1 else 's'}" + (
                f" since {_date(first)}" if first else ""
            )
        context = {
            "table": table,
            "flags": ledger.open_flags(),
            "checks": checks,
            "coverage": coverage,
            "history": history,
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
        query = f"goal={quote(goal)}" + (f"&patron={patron}" if patron is not None else "")
        return RedirectResponse(f"/adventures/{found.id}?{query}", status_code=303)

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
                "model_status": models.status(champion_id),
                "model_evidence": _evidence(models, ledger, champion_id),
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
    return create_app(live=refresher, recompute=Recompute(_RECOMMENDED_GOALS, store=store, executor=executor), store=store)


def _refresh_on_start(live: Refresher, results: Recompute):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        live.start()
        yield
        results.close()

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


def _date(server_time: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(server_time))


def _evidence(models: ChampionModels, ledger: model_status.Ledger, champion_id: int) -> list[dict]:
    """What a champion's Model Status rests on, oldest first: description tests, then checks."""
    reconstruction = models.reconstruction(champion_id)
    if reconstruction is None:
        return [{"text": "Scored on its generic effects only: no reconstruction of its unique effects yet.", "passed": None}]
    tests = reconstruction.description_tests
    rows = [{
        "text": f"Description tests: {len(tests)} ({', '.join(tests)}), kept passing by the test suite."
        if tests else "No description tests yet: it can't reach Verified.",
        "passed": bool(tests),
    }]
    record = ledger.records.get(champion_id)
    for e in record.evidence if record else ():
        rows.append({"text": (f"{_date(e.at)}: " if e.at else "") + e.text, "passed": e.passed})
    return rows


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 120 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"
