# IdleChamps

A personal, local advisor for Idle Champions of the Forgotten Realms. Vocabulary: see `GLOSSARY.md`.

## How to run / test

Python 3.13+ via `uv` (FastAPI + Jinja + htmx, no JS build step). Code lives in `src/advisor/`, tests in `tests/`.

- `uv run pytest`: the whole suite. Offline, no credentials; network connections are blocked in every test.
- `uv run advisor`: live mode at http://127.0.0.1:8000/. Refreshes on start (read-only, allow-listed calls) and from the header's Refresh button. App data folder: `~/Library/Application Support/IdleChampsAdvisor` (`--data-dir` for another), holding `definitions.json.gz`, `snapshot.json.gz`, `history.jsonl.gz` (History Records, written by `history.py` after each successful live Refresh) and an optional hand-written `config.toml` (`request_log = "..."`, `cooldown_seconds = 60`).
- `uv run advisor --from-log`: offline mode on the game's own request log, at http://127.0.0.1:8000/. `--from-log PATH` takes another log or a fixture (`.gz` ok). The host is fixed to 127.0.0.1.
- `uv run advisor-build-fixture [RAW_LOG] tests/fixtures/requestlog.log.gz`: rebuild the test fixture from a recorded log. It keeps allow-listed calls and response bodies only and strips `CREDENTIAL_FIELDS` (`src/advisor/request_log.py`). Never commit a raw `webRequestLog.txt`; it holds credentials.

Test seam (spec #22): drive the app through the `client` fixture in `tests/conftest.py` (FastAPI `TestClient` over `create_app(FIXTURE_LOG)`) and assert on what the page shows. Mark repeated things in templates with `data-row="<kind>"` and read them with `rows(page, kind)`. Expected values come from the real fixture data (e.g. 168 champions, Bruenor at level 300), not from re-running the parser.

Responses missing from the recording (`getcampaigndetails`, `getcompletiondata`) live in the small hand-built `tests/fixtures/progress.log`; `tests/test_explorer.py` merges it over the recording (`read_responses(a) | read_responses(b)` → `write_responses`).

Live mode is tested through the `live` fixture: the app on a temp data folder talking to `tests/fake_game.py`'s `FakeGameServer` (a transport replaying recorded responses; queue one-shot responses or failures with `server.queue(call, ...)`), with a fake clock. Never a real network call.

Layout: `game_client.py` (offline and live clients, read-only call allow-list, the only network code), `refresh.py` (Refresher: one Refresh at a time, cooldown, failure cause/fix), `store.py` (app data folder, atomic gzipped versioned JSON, config), `request_log.py` (log format, credential stripping), `catalogue.py` / `account.py` (typed domain objects parsed from raw responses), `explorer.py` (Formation view tree and completion badges), `web/` (`create_app`, templates; display only), `cli.py`, `fixtures.py`.

## Recommendations: Champion Models, Formation Search, `recommend` (Seam 2)

- `recommendation.py`: `recommend(catalogue, account, adventure, run_goal, patron=None, *, models=None, solver="fast"|"exact"|"exhaustive")` returns a `FormationRecommendation` (fields from spec #22; familiars, modron, hints, comparisons and push_verdict exist but are empty). It is the only module that joins the three below. Seam-2 tests (`tests/test_recommendation.py`) call it on fixture data; `solver="exhaustive"` is the search oracle (tiny instances only); `solver="exact"` is "Prove it's the best" (#31): the fast search, then `exact_search` with it as incumbent, recorded in `FormationRecommendation.exact_solve` ("improved" | "proven" | "timed_out").
- `restrictions.py`: the hard filter. `allows`/`failed`/`describe` on `catalogue.Restriction` (adventure `only_allow_crusaders`/`disallow_crusaders`, patron rules). Anything it can't evaluate counts as not allowed. Forced champions (`Adventure.forced`) bypass it and are seated even when not owned ("Lent by the adventure").
- `champion_models.py`: everything champion-specific lives here. `ChampionModels(catalogue, account)`: `.loadouts(id)` (spec × feat choices), `.scorer(layout, run_goal, main_dps_seat)`, `.status(id)` (Model Status; all Not Modelled until #38/#40), `.assumptions(run_goal)`. `Scorer.score(seats)` gives a log score, the BUD and each seat's `Contribution` (% to the Main DPS, % gold); `Scorer.bound(seat)` must never be below what that seat adds to any formation with that Main DPS (generic effects are separable, so it's exact today). Per-champion reconstructions (#40) and new Run Goal objectives (#29, #32) go behind this interface; if they add interactions between seats, raise `bound` accordingly so the reported gap stays honest.
- `formation_search.py`: champion-agnostic. `search(SearchProblem, scorer_for, budget_s)` fixes the Main DPS and slot, assigns seat numbers to slots (Hungarian, a min-cost-flow special case; ties keep candidate order, which `recommend` sorts by level), hill-climbs on `score`, and reports `gap_pct` from the best assignment bound. `exhaustive_search` is the oracle. `exact_search(problem, scorer_for, time_limit_s=60, incumbent=None)` (OR-Tools CP-SAT) solves one model per Main DPS seat in descending bound order, maximising the sum of integer-scaled `bound`s, scores each solution in full and cuts it off until the solver bound is no better than the best score: exact for any scorer whose `bound` honours its contract, but slow when bounds are loose (many formations to cut off). It never returns worse than the incumbent. The Formation view runs it from the "Prove it's the best" button (`POST /adventures/<id>/prove?goal=&patron=`, replaces the cached recommendation).
- `hints.py`: the one-change engine (#34; Next Steps reuse it). `candidate_changes(catalogue, account, potions=)` lists one-changes (gear chest / Large-contract levels on a slot, feat, unowned champion, +1 modron core level, potion); `what_if` applies one; `measure(catalogue, account, rec, changes, limit=)` prunes with a cheap upper bound from `formation_search.plan_bounds` (needs `rec.problem`), re-runs `recommend` on survivors and returns `Hint(change, gain_pct, recommendation)` best first, zero gains dropped; `hints()` is the rail's top 10. Potions reach the score only through `ChampionModels(..., potions=)`, which only hints pass. The Formation view loads the hints by htmx from `/adventures/<id>/hints`.
- `push_verdict.py`: the damage-only Push Verdict (#32). `recommend` attaches it on Push (`FormationRecommendation.push_verdict`, its assumptions appended). Anchor Run = live instance (`Account.instances`) with the same Main DPS, else highest BUD; damage = anchor BUD × exp(S(formation) − S(anchor formation in play)); wall = first boss the damage × (30 s ÷ cooldown) can't kill on `Campaign.health_curve or Catalogue.health_curve`. Adventure modifiers are classified from `Adventure.game_changes` (neutral / placement / health handler / not available). `placement_known` is always False until the Area Set Cache exists, so Likely is capped at Borderline. `instance_estimate` fills the History Record's `push_estimate`. The recording has no `attack_defines` (cooldown taken as 5 s) or `game_rule_defines` (core curve 10 × 2.031 built in).
- The Formation view computes recommendations on demand per (adventure, goal, patron) and caches them in `create_app`; the Gold Farm and Push tabs show one so far (`_RECOMMENDED_GOALS` in `web/__init__.py`).
- The recording has no `effect_defines`; tests that need ability effects add `EFFECT_TABLES` (hand-written, published shapes) from `tests/test_recommendation.py` to `getdefinitions`.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on AnoukCeyssens/IdleChamps (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` + `docs/adr/`. See `docs/agents/domain.md`.
