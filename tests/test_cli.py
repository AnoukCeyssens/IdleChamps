"""`advisor` command: one command starts the app, bound to localhost only."""

import pytest
import uvicorn
from fastapi.testclient import TestClient

from advisor.cli import main
from conftest import FIXTURE_LOG, rows


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


def test_offline_mode_serves_the_log_on_localhost_only(served):
    main(["--from-log", str(FIXTURE_LOG)])

    [(app, options)] = served
    assert options["host"] == "127.0.0.1"
    assert len(rows(TestClient(app).get("/champions").text, "champion")) == 168


def test_host_cannot_be_widened(served):
    with pytest.raises(SystemExit):
        main(["--from-log", str(FIXTURE_LOG), "--host", "0.0.0.0"])
    assert served == []


def test_without_from_log_it_refuses_until_live_mode_exists(served, capsys):
    with pytest.raises(SystemExit):
        main([])
    assert "--from-log" in capsys.readouterr().err
    assert served == []
