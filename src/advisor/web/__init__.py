"""Web UI: display only, no domain logic."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from advisor.account import parse_account
from advisor.catalogue import ROLE_TAGS, parse_catalogue
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

    return app
