# Strategy knowledge sources for the Formation Advisor

Research for ticket "Which strategy knowledge sources should the advisor trust?" (issue #2).
Checked 2026-11-15. Vocabulary follows `GLOSSARY.md` (Game Definitions, Run Goal, Formation Recommendation, Next Step).

## TL;DR

1. **Roles come from the Game Definitions, so we need no outside source for them.** Every hero in `getDefinitions` has a `tags` array that includes role tags (`dps`, `support`, `tanking`, `healing`, `gold`, `speed`, `positional`) as well as race, class, alignment and affiliation tags. Briv, for example, is tagged `support, tanking, healing, speed` (source: the local `webRequestLog.txt` definitions sample). Synergy inputs such as "buffs adjacent champions" or "buffs all Good champions" are also in the definitions, as ability and effect data, together with the same tags they target.
2. **Emmote's GitHub Pages sites are the best curated, machine-ingestible strategy source.** They are MIT-licensed and were updated within the last week. Formations for Gem Farm and Speed, Briv routes and modron core layouts all ship as JSON or text in public repos.
3. **The fandom wiki is useful for human-written ability explanations, but not as a feed.** It is CC BY-NC-SA and current, but only about one editor is active, and its strategy pages are stale.
4. **ic.byteglow.com is a good viewer and calculator to link to, not a data source.** It is closed-source, has no licence or public API, and builds on the same game endpoints and Emmote data.
5. **We could not find a current, maintained source of standard formations for Push, Complete or Gold Farm.** Reddit and Discord guides and spreadsheets are scattered, dated or inaccessible. This knowledge has to be hand-curated (a small rules file) or derived from the Formation Strength Model.

## Sources evaluated

### 1. Game Definitions (`getDefinitions`, first-party)

- **Covers:** champions, seats, abilities, upgrades and effects (buff targets and amounts), specializations, feats, formation layouts, adventure and variant restrictions. **Role and attribute tags are on every hero**, e.g. the first five heroes in the local sample:
  - `["male","dwarf","fighter","support","neutral","good","companion",...,"positional",...]`
  - `["male","elf","drow","rogue","dps","gold",...]`
  - Briv: `[...,"support","tanking","healing","event","planes","speed",...]`
- **Freshness:** always current, because it is the game's own feed (the same call the client makes).
- **Terms:** game content belongs to Codename Entertainment. Reading it for personal, local use matches what every community tool does. Do not redistribute it.
- **Ingestible:** yes, fully. It is JSON, and we already have it offline in `webRequestLog.txt`. Ticket 02 covers its shape.
- **Gap:** it states facts, not strategy. It says what a champion buffs, not which formation is standard for Gem Farm.

### 2. Emmote's sites (github.com/Emmotes, emmotes.github.io)

Emmote is a long-standing community tool author. byteglow's About page links to these sites, and byteglow itself fetches `ic_modron_library` data. All repos below are MIT (`Copyright (c) 2023 Emmote`). Last push dates come from the GitHub API on 2026-11-15.

| Repo / site | What it gives us | Format | Last push |
|---|---|---|---|
| `ic_scripting_routes` (emmotes.github.io/ic_scripting_routes) | **Gem Farm / Speed**: Briv jump routes by slot-4 item level, stacks calculator, and **Example Formations**: `formsData` in `docs/scripts/forms.js` holds per-campaign Q/M (and E) formations as champion-id arrays per seat, with spec ids. It covers Grand Tour (BBEG builds) and Witchlight (Melf builds), the two standard Briv farm campaigns. `docs/advData/*.json` holds variant/patron area data. | JS object literal + JSON | 2026-11-08 |
| `ic_modron_library` ("Encyclopedia Modronica") | Per-champion stat-node needs and **highly optimised modron core layouts** (`library_ids.json`; per-champion `docs/<champ>.md`, including new champions such as Morte) | JSON + Markdown/HTML | 2026-11-06 |
| `ic_rosters` | Patron eligibility, feats and exclusivity tables; `docs/raw/layouts_*.txt` core layouts (CSV-like: `linkHash,coreId,dps,str,...`) | text/CSV | 2026-11-14 |
| `ic_wiki` | `docs/championData.json` (4.3 MB): per-champion record with `roles`, `seat`, `patrons`, attacks, etc. Mostly reshaped Game Definitions plus curation (patron notes). README: "work in progress … Do NOT expect me to finish". | JSON | 2026-11-14 |
| `ic_spoilers`, `ic_servercalls` | Upcoming champions; server-call reference (useful for ticket 02) | site | 2026-11-14 / 11-11 |
| Google Sheets linked from emmotes.github.io | Game Speed Effects calculator, Potion Sustain, Oxventure/Prudence formation strategy, etc. | Sheets | not dated |

- **Coverage:** strong for Gem Farm and Speed (formations, routes, stacking, modron cores). Nothing on Push, Complete or Gold Farm formations.
- **Freshness:** excellent. Pushes landed in the last 1–2 weeks, and newly released champions (Morte) are already present.
- **Terms:** MIT, so we can copy or vendor it with attribution.
- **Ingestible:** yes. Fetch raw files from `raw.githubusercontent.com`. Caveat: `formsData` sits inside a JS file, so extract the object literal with a JSON parser on the matched text and never `eval` it. Pin by commit SHA and treat the schema as unstable, since these are personal sites without an API contract.

### 3. Briv gem-farm scripts (open source)

- `antilectual/Idle-Champions` ("Script Hub", MIT, last push 2026-06-02) and `Emmotes/ScriptHub` (fork with all addons, 2026-11-14), plus `Emmotes/BrivMaster` (Irisiri's script, no licence).
- **Value to us:** they encode the **community-standard Gem Farm formation structure** (`AddOns/IC_BrivGemFarm_Performance/README.md`):
  - Q = speed formation with Briv and at least one familiar on the field;
  - W = stacking formation without familiars, one that must not kill;
  - E = speed formation without Briv, for boss zones at 4/9 jump.
  - They also give the stack-zone rule of thumb: stack at reset zone − 4/6/8/10 for 1/2/3/4 skip.
- **Not reusable as code.** The scripts are Windows AutoHotkey, read process memory and make write calls, all of which are out of scope for us. Use them as documentation of conventions only.
- `djravine/idlecombos` (MIT, 2026-05-11): an AHK account utility with a Briv stack calculator. Low value for us.

### 4. Idle Champions fandom wiki (idlechampions.fandom.com)

- **Covers:** a page per champion, with an infobox (`role = Tanking / Healing / Support / Speed`, seat, patrons) and prose on abilities, specs and feats. There are also mechanics pages (Gear, Familiars, Chests).
- **Freshness:** recent edits are from 2026-11-11 to 11-13 and include the newest champion, Morte. But the MediaWiki API reports `activeusers: 4`, and recent changes are almost all from one editor, so the bus factor is low. Strategy pages are stale. `Formation_strategy` is a `{{stub}}` that states: "This page is severely outdated and most of these formations aren't possible anymore." The Guides category has six pages, none of them formation guides.
- **Terms:** **CC BY-NC-SA** (from the `siteinfo` rightsinfo API). Non-commercial personal use is fine. Attribution and share-alike apply if we redistribute derived text.
- **Ingestible:** partly. `api.php?action=parse&prop=wikitext` works without auth (the HTML front page returned HTTP 402 to our fetcher). The infobox templates parse cleanly, but prose does not. Roles duplicate the definition tags.
- **Use:** an optional hand-reading reference for ability nuances. Don't use it as a feed.

### 5. ic.byteglow.com ("Idle Champions Utils")

- **Covers:** account viewers that use your user data (champions, formations, variants, patrons, modron, events, legendaries, feats), a formation builder with shareable images (`f.byteglow.com/f/...`), and a Briv speed/stack calculator at `/speed` (recommended in the Script Hub README).
- **Data:** calls the official play servers (`getDefinitions`, `getcampaigndetails`) and fetches Emmote's modron library. It adds no independent strategy knowledge (no role or synergy curation found in the bundle).
- **Freshness:** "The site is updated regularly, and often without notice or changelog entries" (About page).
- **Terms:** no licence, closed-source SPA, no documented public API. About page: user data "will only be stored by your browser".
- **Ingestible:** no, and we should not scrape it. Link to it from the UI for calculators instead (e.g. a "check stacks" link next to a Gem Farm recommendation).

### 6. Reddit, Discord, spreadsheets, guide sites

- **r/idlechampions wiki:** has a community resources index (e.g. a Champions Overview spreadsheet by /u/ThemoKenwood, All Formation Layouts by /u/FieryCrystal106, and Psylisa's champion evaluations). The wiki itself says these are community-created and not maintained by Codename. We could not verify recency, because Reddit blocks unauthenticated fetches and the mirror failed.
- **Official Discord:** has guide and scripting channels where Emmote and the byteglow author are active. It requires login, so content can't be ingested and the terms are unclear. Treat it as a place a human reads.
- **Spreadsheets:** individual Google Sheets (variant guides, damage calculators), often from 2018–2020. Steam threads note they go unmaintained (e.g. "not sure if he updates it anymore"). Some are linked from Emmote's index and are current but narrow.
- **Guide sites** (Gamers Decide 2020, TouchTapPlay, earlyguides): dated, generic, often wrong for today's meta. **Exclude them.**
- `idle.kleho.ru` (an older variant site): returned 404. Treat it as dead.

## Assessment matrix

| Source | Roles | Synergies | Standard formations per Run Goal | Fresh | Reuse terms | Machine-ingestible |
|---|---|---|---|---|---|---|
| Game Definitions | **yes (tags)** | raw effect data | – | always | personal use | **yes** |
| Emmote repos | yes (ic_wiki) | partial (modron stat nodes) | **Gem Farm / Speed** | days | **MIT** | **yes** (pin SHA) |
| Script Hub docs | – | – | Gem Farm structure (Q/W/E) | months | MIT | docs only |
| Fandom wiki | yes (infobox) | prose | stale | days (1 editor) | CC BY-NC-SA | partly (API) |
| byteglow | – | – | – | regular | none stated | no |
| Reddit/Discord/sheets | varies | prose | scattered | unknown–stale | unclear | no |

## Recommended primary source set

1. **Game Definitions as the primary source for roles and synergy mechanics.** Derive the role from hero `tags` (`dps`, `support`, `tanking`, `healing`, `gold`, `speed`, `positional`). Derive synergy from effect definitions: buff targets by tag, adjacency and column. This is always current and needs no third-party trust. It feeds ticket 03 (Formation Strength Model).
2. **Emmote's repos as the curated layer for Gem Farm and Speed** (and familiar and modron core setup):
   - `ic_scripting_routes`: the `formsData` example formations, the routes, and the stacks rules;
   - `ic_modron_library` and `ic_rosters/raw/layouts_*.txt`: modron core layouts.

   Vendor snapshots pinned to a commit SHA, record attribution (MIT), and add a "refresh source" step that diffs against upstream. If Speed and Gem Farm resolve to the same Briv-based Q formation here, that supports collapsing them, as the map's note suggests.
3. **A small hand-curated rules file in this repo** for what no maintained source provides: per-Run Goal intents (Push = maximise DPS and survival; Complete = satisfy variant and patron restrictions, then Push logic; Gold Farm = gold-find champions (`gold` tag) plus DPS; Gem Farm = Script Hub's Q/W/E structure) and a few known "core" champions per goal. Each rule cites its source (an Emmote file, the Script Hub README, or a wiki page).
4. **Human-reference links only (not ingested):** byteglow (`/speed`, `/formation`), the fandom champion pages, and Emmote's Google Sheets. Surface them in the UI as "learn more" links.
5. **Excluded:** generic guide sites, undated spreadsheets, Discord and Reddit content as data.

**Risks:** the Emmote data is a single-maintainer, personal, schema-less project, so pin it and validate on refresh. The Gem Farm example formations cover only the two standard Briv farm campaigns. We found no reliable external source for Push or Gold Farm formations, so quality for those Run Goals rests on the Formation Strength Model (ticket 03), checked against the player's own saved formations.

## Sources

- Local Game Definitions sample: `~/Library/Application Support/Steam/.../downloaded_files/webRequestLog.txt` (hero `tags`)
- https://github.com/Emmotes and https://emmotes.github.io/ (repos listed above; GitHub API metadata 2026-11-15)
- https://raw.githubusercontent.com/Emmotes/ic_scripting_routes/main/docs/scripts/forms.js (`formsData`)
- https://raw.githubusercontent.com/antilectual/Idle-Champions/main/AddOns/IC_BrivGemFarm_Performance/README.md
- https://idlechampions.fandom.com/api.php?action=query&meta=siteinfo&siprop=rightsinfo|statistics ; `Formation_strategy`, `Briv`, `Morte` wikitext via `action=parse`
- https://ic.byteglow.com/ (JS bundle: routes, About text, endpoints)
- https://github.com/djravine/idlecombos
- Reddit wiki index and Steam discussions via web search (https://steamcommunity.com/app/627690/discussions/), not directly fetchable
