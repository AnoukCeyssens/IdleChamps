"""Shared test seam: the FastAPI app in offline mode, fed by the credential-free fixture log.

`client` is the primary seam (spec #22, Seam 1). Every test runs with outbound network
connections blocked, so offline mode provably never calls the game servers.
"""

from __future__ import annotations

import socket
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from advisor.web import create_app

FIXTURE_LOG = Path(__file__).parent / "fixtures" / "requestlog.log.gz"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(create_app(FIXTURE_LOG))


class _Rows(HTMLParser):
    """Collects the elements marked `data-row="<kind>"` with their classes and visible text."""

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self.rows: list[dict] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._depth:
            self._depth += 1
        elif attrs.get("data-row") == self.kind:
            self._depth = 1
            self.rows.append({"classes": set((attrs.get("class") or "").split()), "text": ""})

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            row = self.rows[-1]
            row["text"] = " ".join((row["text"] + " " + data).split())


def rows(html: str, kind: str) -> list[dict]:
    """The rows of one kind on a page, e.g. `rows(page, "champion")`."""
    parser = _Rows(kind)
    parser.feed(html)
    return parser.rows
