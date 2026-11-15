# IdleChamps

A personal, local advisor for Idle Champions of the Forgotten Realms. Vocabulary: see `GLOSSARY.md`.

## How to run / test

Python 3.13+ via `uv` (FastAPI + Jinja + htmx, no JS build step). Code lives in `src/advisor/`, tests in `tests/`.

- `uv run pytest`: the whole suite. Offline, no credentials; network connections are blocked in every test.
- `uv run advisor --from-log`: offline mode on the game's own request log, at http://127.0.0.1:8000/. `--from-log PATH` takes another log or a fixture (`.gz` ok). The host is fixed to 127.0.0.1.
- `uv run advisor-build-fixture [RAW_LOG] tests/fixtures/requestlog.log.gz`: rebuild the test fixture from a recorded log. It keeps allow-listed calls and response bodies only and strips `CREDENTIAL_FIELDS` (`src/advisor/request_log.py`). Never commit a raw `webRequestLog.txt`; it holds credentials.

Test seam (spec #22): drive the app through the `client` fixture in `tests/conftest.py` (FastAPI `TestClient` over `create_app(FIXTURE_LOG)`) and assert on what the page shows. Mark repeated things in templates with `data-row="<kind>"` and read them with `rows(page, kind)`. Expected values come from the real fixture data (e.g. 168 champions, Bruenor at level 300), not from re-running the parser.

Responses missing from the recording (`getcampaigndetails`, `getcompletiondata`) live in the small hand-built `tests/fixtures/progress.log`; `tests/test_explorer.py` merges it over the recording (`read_responses(a) | read_responses(b)` → `write_responses`).

Layout: `game_client.py` (offline client, read-only call allow-list), `request_log.py` (log format, credential stripping), `catalogue.py` / `account.py` (typed domain objects parsed from raw responses), `explorer.py` (Formation view tree and completion badges), `web/` (`create_app`, templates; display only), `cli.py`, `fixtures.py`.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on AnoukCeyssens/IdleChamps (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` + `docs/adr/`. See `docs/agents/domain.md`.
