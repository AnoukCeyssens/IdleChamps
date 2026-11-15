# Game API and data shape (Idle Champions)

Research for ticket 02 ("What does the game's API give us, and how do we call it read-only?").
Researched 2026-11-15.

**Credential rule.** Every user id, hash, instance id/key, Steam id/name and auth ticket below is
shown as `<redacted>`. Only key names, types and counts were extracted from the local log.

## Sources

| Tag | Source | Trust |
| --- | --- | --- |
| **LOG** | Local `webRequestLog.txt` (game client v659, macOS Steam build). One session: `getPlayServerForDefinitions`, `getDefinitions`, `authenticatesteamuser`, `initializemobilesession`, `getuserdetails`, each with full JSON response. Inspected with `python3 -I` printing structure only. | Primary (real responses) |
| **EMM** | [Emmotes/ic_servercalls](https://github.com/Emmotes/ic_servercalls) `docs/scripts/serverCalls.js` (v3.101, MIT, pushed 2026-11-11) and its tabs, e.g. `tab_createFormations.js`, `tab_eventTiers.js`, `util_campaigns.js`. The de-facto community client. | Strong secondary (working code) |
| **CHD** | [chetanddesai/idle-champs](https://github.com/chetanddesai/idle-champs) `docs/server-calls.md` and `README.md`: endpoint catalogue derived from EMM plus empirical notes from live calls (2026). | Secondary (says it is empirical) |
| **SH** | [mikebaldi/Idle-Champions](https://github.com/mikebaldi/Idle-Champions) `ServerCalls/IC_ServerCalls_Class.ahk` (Script Hub, the main gem-farm automation). | Strong secondary (working code) |
| **XEIO** | [Xeio/IdleCodeRedeemer](https://github.com/Xeio/IdleCodeRedeemer) `src/shared/idle_champions_api.ts`, `src/lib/player_data.d.ts` (TypeScript typings of `getuserdetails`). | Secondary |

Codename Entertainment publishes no API docs. Everything here is from the game's own traffic plus community clients.

## 1. Transport and endpoints

- **Shape.** `GET {server}post.php?call={name}&{params}`. `{server}` is `https://<host>.idlechampions.com/~idledragons/`. The game client also sends some calls as POST: in the LOG, `getDefinitions` has only `call=` in its URL. Community clients use GET for everything. (LOG, EMM `sendOutgoingCall`)
- **Boilerplate params** that community clients append to every call: `language_id=1`, `timestamp=0`, `request_id=0`, `mobile_client_version=99999`, `include_free_play_objectives=true`, `instance_key=1`, `offline_v2_build=1`, `localization_aware=true` (EMM `appendBoilerplate`; SH and XEIO use the same set). The real client sends `network_id=11` (Steam), `mobile_client_version=659` and incrementing `request_id`/`timestamp` (LOG). The server accepts the community values.
- **Auth params:** `user_id=<redacted>`, `hash=<redacted>`. Most calls other than `getuserdetails` also need `instance_id=<redacted>`, which comes from `getuserdetails.details.instance_id` (EMM `appendInstanceId`).

### Play-server discovery
1. `GET https://master.idlechampions.com/~idledragons/post.php?call=getPlayServerForDefinitions` (no auth). The response has `play_server` (in the LOG a `psltN.idlechampions.com` host), plus `analytics_server` and `use_individual_def_checksums` (LOG).
2. Any response may carry `switch_play_server: <url>`, sometimes with `success:true` and no payload. The client must swap servers, upgrade `http://`→`https://`, and replay the call (EMM `sendServerCall`, CHD "Observed request flow").
3. Hosts get renamed or retired (`psN` → `psltN`). Use a timeout, and on a network failure re-run discovery once (CHD "Dead-shard recovery").

### Read-only calls useful to the advisor

| Call | Params beyond boilerplate | Gives | Source |
| --- | --- | --- | --- |
| `getPlayServerForDefinitions` | none (master host) | play server URL | LOG, EMM |
| `getuserdetails` | `user_id`, `hash` | The whole **Player Snapshot** (section 2), plus an embedded `defines` subset and `instance_id` | LOG, EMM, SH |
| `getdefinitions` | `supports_chunked_defs=0`, `new_achievements=1`, `challenge_sets_no_deltas=0`; optional `filter=a_defines,b_defines` **or** `checksum=` | **Game Definitions** (section 3). Unfiltered is about 19 MB (CHD). With `filter` you get just the named tables. With `checksum` you get only changed tables (the LOG response is such a delta: 4 tables returned, `table_checksums` lists 201). | EMM `getDefinitions`, CHD, LOG |
| `getallformationsaves` | auth + `instance_id` | `all_saves` (every formation save, all campaigns) and `formation_objects`, keyed by campaign id: `campaign_id`, `campaign_name`, `is_event_campaign`, `patron_id`, `campaign_changes`, `game_change_data` (includes the `formation` layout and `slot_escort` blocked slots) | EMM `getFormationSaves` + `tab_createFormations.js` `cf_buildCampaigns` |
| `getcampaigndetails` | auth + `instance_id` | "campaign details for the account". The response shape is **not documented anywhere public**. It is the likeliest source of per-adventure / variant / patron completion. | EMM, CHD |
| `getcompletiondata` | auth + `instance_id`, `level=0` | `data.event[heroId].v[tier][variant]` (event tier completion per champion) | EMM `tab_eventTiers.js` |
| `getpatrondetails` | auth + `instance_id` | patron unlock/progress, influence, shop | EMM, CHD |
| `geteventsdetails` | auth + `instance_id` | current/next events, event champions | EMM, CHD |
| `getlegendarydetails` | auth + `instance_id` | legendary items per hero/slot (also embedded in `getuserdetails.details.legendary_details`) | EMM, CHD |
| `getusergameinstance` | `game_instance_id` | one party/instance | EMM |
| `getmasterychallengesdata`, `getactivetasksdata`, `getbastiondetails`, `getdailyloginrewards`, `getshop` | auth + `instance_id` | not needed for v1 | EMM |

The advisor must **never** call anything else. Mutating calls in the same catalogue include `setcurrentobjective`, `softreset`, `SaveFormation`, `saveModron`, `opengenericchest`, `buysoftcurrencychest`, `useServerBuff`, `purchasefeat`, `redeemcoupon`, `convertresetcurrency`, and every `trials*`, `craft*`, `upgrade*` and `purchase*` call (CHD table of contents). Recommendation: hard-code an allow-list of the read calls above.

### Error handling (EMM `sendServerCall`, CHD "Error Handling")
- `failure_reason: "Outdated instance id"`: call `getuserdetails` again and retry.
- `"Security hash failure"`: the credentials are wrong or stale. Stop; don't retry.
- `"non-atomic"`: interrupted by in-game activity. Report it and let the user retry.
- HTTP 404/500/502 means the shard is dead: rediscover. EMM caps retries at 4 and waits 200 ms after each call.

## 2. Player Snapshot: `getuserdetails` (LOG)

Top level: `success`, `details` (146 keys), `defines` (23 tables), `current_time`, `linked_accounts`, `current_sales`, `actions`, `debug_*`, plus server stats.
`details` also holds `instance_id`. That value is a credential: drop it after use, never persist it.

| Need | Field(s) in `details` | Shape / notes |
| --- | --- | --- |
| Owned champions, level | `heroes[]` | `hero_id`, `level`, `owned` ("1"/"0"), `in_seat`, `upgrades[]` (purchased upgrade ids), `health`, `skin_id`. **Numbers arrive as strings.** Each entry also has a `user_id` field: strip it. |
| Specializations | `heroes[].specialization_choices[]` | upgrade ids of the chosen specs. Names come from `upgrade_defines[].specialization_name`. |
| Feats | `heroes[].unlocked_feats[]`, `active_feats[]`, `feat_slots`, `feat_slot_unlocks[]`, `legendary_feats[]`, `feat_levels[]` | feat ids → `hero_feat_defines` |
| Gear (loot) | `loot[]` | `hero_id`, `slot_id` (1–6), `rarity` (1 common … 4 epic), `enchant` (item level offset; ilvl ≈ enchant+1, per community convention), `gild` (0 none, 1 shiny, 2 golden), `pigment`. Effects → `loot_defines_N[].effects[].effect_string`. |
| Legendary items | `legendary_details.legendary_items[heroId][slotId]`, `costs_by_hero` | see CHD for semantics |
| Familiars | `familiars[]` | `familiar_id`, `assignment` (e.g. `{Clicks: n, game_instance_id}`). Each entry has a `user_id`: strip it. |
| Saved formations | `formation_saves_v2[]` (only the current campaign: `formation_saves_v2_campaign_id`, max `formation_saves_v2_max`) | `formation_save_id`, `campaign_id`, `name`, `favorite`, `formation[]` (hero id per slot index, `-1` = empty), `specializations{heroId:[upgradeIds]}`, `feats{heroId:[featIds]}`, `familiars{Clicks,Seat,Ultimates,UltimateSpecific,BuffType,BuffSpecific,AutoProgress,...}`. Use **`getallformationsaves`** for every campaign. The legacy `formation_saves[]` is empty. |
| Current formation | `formation[]` (10 slots); per party `game_instances[].formation`, `hero_in_seats{seat:heroId}` | |
| Modron cores | `modron_saves{coreId}` | `core_id`, `grid` (17 rows of tile ids), `formation_saves{campaignId: formationSaveId}`, `area_goal` (string), `buffs[]`, `properties.toggle_preferences{formation,reset,buff}`, `instance_id`, `user_id` (both credential-like: strip). Also `modron_components`, `stats.highest_modron_core_level`, `stats.modron_cores_unlocked`, `tiles[]` (`tile_id`,`count`). |
| Parties / instances | `game_instances[]` | per party: `current_adventure_id`, `current_patron_id`, `current_patron_tier`, `current_area`, `highest_area`, `buffs`, `stats` |
| Currencies | `red_rubies` (gems), `red_rubies_spent`, `hard_currency`, `reset_currencies[]` (favor: `currency_id`, `current_amount` as a **string in sci notation**, `total_earned`, `total_spent`), `potion_reagents`, `legendary_vessels`, `stats.multiplayer_points` (Scales of Tiamat, per CHD) | |
| Chests | `chests{chestTypeId: count}`; `stats.chests_opened_type_N` | names → `chest_type_defines` |
| Buffs / potions / contracts | `buffs[]` (`buff_id`, `inventory_amount`, `remaining_time`, …) | → `buff_defines` |
| Patrons | `patrons[]` (`patron_id`, `unlocked`, `meets_requirements[]`), `current_patron_id`/`_tier`, `patron_perks[]` | more detail via `getpatrondetails` |
| Adventure progress | **No per-adventure completion list in `getuserdetails`.** Aggregates live in `stats`: `highest_area_completed_ever_c<campaignId>`, `campaign_<id>_adventures_completed`, `campaign_<id>_variants_completed`, `adventure_<id>_variants_completed`, `completed_adventures_variants_and_patron_variants_c<id>`, `unique_adventures_completed`, `unique_variants_completed`, `free_plays_completed` | 668 `stats` keys in total, many of them champion-specific mechanic counters (e.g. `ellywick_*`, `kthriss_*`). |
| Events | `event_details[]` (active event: `hero_ids`, `completed_adventure_ids[]`, `user_data.event_tokens`, `unlocked_years`), `events_details{tokens, active_events, next_event, champion_details, …}`, `next_event` | `completed_adventure_ids` gives per-adventure completion for the active event only |
| Unlocked features | `unlocked_features[]` (e.g. `modrons`, `familiars`, `formation_saves`, `campaign_N`) | gating for which advice applies |
| Trials, time gates, bastion, seasons | `trials_data`, `time_gates`, `bastion_details`, `season_details` | out of scope v1 |

`getuserdetails.defines` is an embedded subset of definitions. The LOG has 124 `adventure_defines`, 4 `campaign_defines`, 207 `upgrade_defines`, 576 `loot_defines`, `reset_currency_defines` (4) and `event_v2_defines`. It looks scoped to the account's current or new content, so don't rely on it being complete: fetch `getdefinitions` for the full catalogue. (CHD says `reset_currency_defines` doesn't exist. The LOG shows it inside `getuserdetails.defines`, but not in `getdefinitions`' `table_checksums`.)

## 3. Game Definitions: `getdefinitions` (LOG `table_checksums` + embedded `defines`)

Tables available (201 checksum entries). The large ones are sharded: `upgrade_defines_0..86`, `loot_defines_0..17`, `effect_defines_0..12`, `graphic_defines_0..31`. Unsharded names such as `upgrade_defines`, `loot_defines` and `effect_defines` work in `filter` (EMM uses `upgrade_defines`; CHD uses `hero_defines,attack_defines,...`). Tables relevant to the advisor:

- **`adventure_defines[]`**: `id`, `campaign_id`, `location_id`, `area_set_id`, `name`, `description`, `requirements_text`, `objectives_text`, `restrictions_text` (human-readable), `requirements[]` (`condition`: `adventure_complete`, `hero_available`, `event_year_unlocked`, `adventures_completed`, `date`), `objectives[]` (`{condition:"complete_area", area}`), **`patron_objectives{patronId:{"1":[{condition:"complete_area",area}]}}`** (e.g. Mirt 250, Vajra 275, Strahd 300, Zariel 325, Elminster 825 in one sample), **`variant_adventure_id`** (parent adventure of a variant; 97 of 124 sampled are variants), `rewards[]`, `rewards_by_tier`, `repeatable`, `costs`, and **`game_changes[]`** (below).
- **`game_changes[].type`** values seen in the LOG (count): `formation` (124, a layout override), `formation_saves_campaign_id` (120), `initial_formation` (50), `force_use_heroes` (48: `hero_ids`, `forced_slots`, `start_levels`), `only_allow_crusaders` (20: `by_ids`/`by_tags`/`by_stat`/`by_expr` such as ``HasTag(`female`) || HasTag(`evil`)``), `disallow_crusaders`, `disallow_ultimates`, `force_allow_hero`, `slot_escort` / `slot_escort_by_area` (blocked/escort seats: `slot_id(s)`), `slot_effects` (`effects[{effect_string}]` applied to `slots[]`), `hero_effects`, `global_effects`, `random_crusader_damage`, monster modifiers (`add_monster_properties(_by_tag)`, `random_monster_waves`, `guaranteed_monster(s)`, `additional_bosses(_by_area)`, `replace_monsters_by_area`, `effect_strength_by_area`, `monster_base_stats`, `active_monster_limit`, `stacking_effect`, `weather`, `darken_by_area`), `quest_requirement_mult`, `unlocked_upgrades`, cosmetic ones (`force_skin`, `gold_icons`, `buff_display_by_area`). These are machine-readable restrictions; `restrictions_text` is the human summary.
- **`campaign_defines[]`**: `id`, `name`, `short_name`, `reset_currency_id` (favor), `requirements`, `properties.is_event`/`event_id`, and `game_changes[]`, whose `formation` entry is the **campaign's default seat layout**.
- **Formation layout** (`game_changes[type=formation].formation[]`): an array indexed by slot; each slot is `{x, y, col, adj:[slotIndex…]}`. `col` 0 = front column (largest `x`). `adj` is the **adjacency list** (example Grand Tour 10-slot layout: slot 0 `{x:80,y:20,col:0,adj:[1,2]}` … slot 9 `{x:0,y:40,col:4,adj:[6,7,8]}`). The adventure's own `formation` overrides the campaign's (EMM `cf_extractFormationLayout`). Blocked seats come from `slot_escort` (EMM `cf_extractBlockedSlots`). Formation saves are keyed by the campaign id from `formation_saves_campaign_id` when present (EMM `cf_buildChampions`).
- **`hero_defines[]`**: `id`, `name`, `seat_id`, `base_cost/damage/health`, `base_attack_id`, `ultimate_attack_id`, **`tags[]`** (race, class, gender, alignment, role such as `dps`/`support`/`healer`/`tank`/`gold`/`speed`, `positional`, `evergreen`/`event`). `only_allow_crusaders` checks these tags. Also `character_sheet_details` (class, race, alignment, ability scores), `default_feat_slot_unlocks`, `adventure_ids` (`{type:"base", adventure_id}`), `event_name`, `is_available`, `date_available`, `last_rework_date`, `properties.legendary_effect_id`.
- **`upgrade_defines`**: `id`, `hero_id`, `required_level`, `required_upgrade_id`, `upgrade_type` (`unlock_ability`, `unlock_ultimate`, …), `effect` (e.g. `"effect_def,2973"` → `effect_defines`), `specialization_name`/`_description` (marks spec choices), `name`, `tip_text`, `static_dps_mult`.
- **`effect_defines`**: champion ability effects referenced by `effect_def,<id>`. The effect DSL is not documented: strings like `health_mult,100`, `monster_damage_increase,100`, `hero_dps_multiplier_mult,…`. `effect_key_defines` holds the key descriptions.
- **`hero_feat_defines`**, **`familiar_defines`**, **`modron_core_defines`**, **`modron_tile_defines`**, **`patron_defines`**, `patron_perk_defines`, `attack_defines`, `legendary_effect_defines`, `loot_defines` (`hero_id`, `slot_id`, `rarity`, `effects[{effect_string}]`), `chest_type_defines`, `buff_defines`, `stat_defines`, `event_v2_defines` (`static_details.campaign_id`), `game_rule_defines`, `monster_defines`, `adventure_area_defines` (waves/monsters per area).

## 4. Community norms and limits

- **No published API, ToS clause or rate limit found.** I couldn't locate Codename Entertainment's terms online (codenameentertainment.com has no reachable terms page; search found nothing). Treat it as unofficial, unsupported and changeable without notice.
- **Established tooling relies on it**: Script Hub (SH), Emmote's server-calls site (EMM), byteglow (`ic.byteglow.com`, linked from EMM), code redeemers (XEIO). All of them take the user id + hash from the in-game **Settings → Support → Copy Support URL** (`user_id=…&device_hash=…`) (CHD README). That gives the advisor a credential source other than the log.
- **Credential hygiene** (CHD README): "Treat `hash` as a full-account credential." It grants write access to the account, so the read-only guarantee has to live in our code (allow-list).
- **Pacing**: EMM sleeps 200 ms after every call and caps retries at 4. CHD caches the play server for 24 h and calls `getdefinitions` filtered, once per refresh. No 429-style throttling has been reported. An on-demand refresh (one `getuserdetails` + a filtered `getdefinitions` + `getallformationsaves`, with definitions cached by checksum) is well within what existing tools do.
- **Versioning**: `mobile_client_version=99999` is accepted. Response fields grow over time; feature flags such as `y1_rebalance`, `use_new_save_handler` and `formation_saves_v2` show schema migrations. Parse defensively: numbers often come as strings, and big numbers as sci-notation strings.
- **Interaction with the running game**: `getuserdetails` returns a fresh `instance_id`, and EMM handles `Outdated instance id` by re-fetching. Whether a third-party `getuserdetails` invalidates the running client's session is **not documented**. Script Hub calls it while the game runs, which suggests it's tolerated. Avoid calling during a gem-farm loop anyway.

## 5. Open questions (for a first spike, one live read call each)

1. **`getcampaigndetails` response shape**: does it list completed adventure ids / variants / patron variants per campaign? It's the main gap for "which adventures/variants are left". Fallback: `stats` aggregates plus `event_details[].completed_adventure_ids`.
2. Whether the effect DSL (`effect_defines`) is tractable enough to compute formation strength, or whether ticket 03 needs curated strategy data instead.
3. Exact `getdefinitions` filter names for the sharded tables (does `upgrade_defines` merge all shards? EMM's usage suggests yes).

## 6. Recommended call sequence for the advisor

1. Read `user_id`/`hash` (in memory only) from the log's latest `getuserdetails` URL, or from a Support URL the user pastes.
2. `getPlayServerForDefinitions` (cache the host for the session).
3. `getuserdetails`: the Snapshot. Strip `user_id`, `instance_id`, `hash` and similar fields before storing anything.
4. `getdefinitions?filter=hero_defines,upgrade_defines,hero_feat_defines,adventure_defines,campaign_defines,patron_defines,familiar_defines,modron_core_defines,effect_defines,effect_key_defines,loot_defines,attack_defines,chest_type_defines,buff_defines`. Cache it; use `checksum` or refetch rarely.
5. `getallformationsaves` (+ `getcampaigndetails`, `getpatrondetails` once item 1 above is verified).
6. Handle `switch_play_server` and `Outdated instance id`; send no other calls.
