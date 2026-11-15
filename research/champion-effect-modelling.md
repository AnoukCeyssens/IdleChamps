# Can every champion's unique effect be modelled exactly?

Research for issue #10. The question: for each class of mechanic that a generic interpreter can't handle, where can its exact behaviour be found, and how faithfully can it be modelled? It also asks how effects stack, how large a full champion-adapter catalogue would be, what it costs to keep up to date, and which champions cannot be modelled at all. Builds on [#4 / `research/formation-strength-model.md`](https://github.com/AnoukCeyssens/IdleChamps/blob/research/formation-strength-model/research/formation-strength-model.md). This file gives facts and options. It does not make the decision.

Date: 2026-11-15. Game client v659 (1.242.1). Vocabulary follows `GLOSSARY.md`.

## Sources

| ID | Source | Trust |
|----|--------|-------|
| GD | **Game Definitions**: the full `getDefinitions` payload cached by the client as `downloaded_files/cached_definitions.json` (21 MB). The `webRequestLog.txt` copy is only a 4-table delta. Analysed with throwaway `python3 -I` scripts. | Primary |
| LOG | `webRequestLog.txt`, `getuserdetails` response. Only **key names** were read (e.g. the 668 `details.stats` names). No values were read, and no credentials were read or copied. | Primary |
| CL | The game client's managed assembly `IdleDragonsMac.app/.../Managed/Assembly-CSharp.dll` (15 MB, Mono build). Only **type-name strings** were listed with `strings`. Nothing was decompiled, and no code was copied. | Primary (existence of handlers only) |
| SP | [Emmotes/ic_spoilers](https://github.com/Emmotes/ic_spoilers) (MIT, pushed 2026-11-14): raw effect data for upcoming champions and reworks, annotated (e.g. "This ability is prestack"). | Strong secondary |
| SH | [mikebaldi/Idle-Champions](https://github.com/mikebaldi/Idle-Champions) `AddOns/IC_Core/MemoryRead/HeroHandlers/` (Script Hub, MIT, pushed 2025-11-10) | Primary for what it reads |
| EM, BG | Emmotes route planner and byteglow, as described in #4 | See #4 |
| WK | Fandom wiki: still unreachable to automated fetches (HTTP 402, see #4). Not cited. | n/a |

Every count below was computed over GD on 2026-11-15 and will drift with game updates. The classifications in §2 are **keyword heuristics** over effect JSON, so treat their counts as ±10%, not exact.

## 1. Where champion behaviour lives

Each champion effect in GD is split across five layers. Only the last one holds the exact rule for champion-unique mechanics.

1. **Parameters in data.** `effect_defines.effect_keys[]` holds 3,089 effects. Each key is an `effect_string` (`key,param,…`) plus optional fields. Those fields have a very long tail: **1,168 distinct field names**, of which **857 appear at most twice**. Examples: `knight_odds`, `seconds_of_bud`, `max_bees`, `shadow_reaver_sequences`. Most of these are one champion's private configuration. [GD]
2. **The effect-key catalogue.** `effect_key_defines` (1,023 rows) gives `param_names`, an `owner` and a description template.
   - **Only 607 of the 1,210 keys used in `effect_defines` (50%) have a catalogue entry.**
   - Of the 684 keys used by exactly one champion, 252 have an entry and 196 name their params.
   - Some entries are explicitly empty. For example `briv_unnatural_haste` has `param_names: amount,int required_stacks,stack_consume_percent,skip_chance,skip_amount`, but its description is "I'm but a mere handler". [GD]
3. **Effect description text.** Descriptions are templated (`$amount`, `$(not_buffed amount)`, conditional variants). They often state the stacking rule in words:
   - "stacking multiplicatively" appears in 502 effects;
   - "stacking additively" in 137;
   - "additively and then applying multiplicatively" in 10;
   - "capping at" in 18;
   - "chance" in 159.

   27 champion-owned effects have an empty description. This text is the most complete first-party *specification* of unique mechanics, but it is prose. Rounding, ordering and edge cases are not stated. [GD]
4. **Patch notes.** `changelog_defines` in GD holds the last 16 release notes (2026-08-13 → 2026-11-14). They name mechanic changes, including **behaviour-only fixes that change no data**. Example, v655: "Changed Ishi's treasure hunter ability to work as intended, resetting only when Ishi attacks and kills an enemy…". [GD]
5. **Client code: the only exact source.** The macOS client is a Unity **Mono** build, so `Assembly-CSharp.dll` is managed IL and could be decompiled. Its strings include **944 `*Handler` type names**. **404 of them start with a champion's name, covering 139 champions**: `BrivUnnaturalHasteHandler`, `BrivSteelbonesHandler`, `JangSaoStarryFormHandler`, `PenelopeInsectPlagueHandler`, and so on. Voronika has 10, and Lucius, Melf and Selise have 9 each. [CL]
   - **Caveats:** decompiling the client was not attempted. Doing so may breach the game's terms of service, and that was not assessed. The game's code could not be published in this public repo in any case.

Community and other sources add no new *rules*, with two exceptions:

- **Briv:** EM's closed-form skip and stack math (see #4) fills exactly the gap GD leaves. GD has the param names but no mapping from gear to `skip_chance`/`skip_amount`.
- **Speed modules:** byteglow hand-codes them for 11 champions (#4).

The other sources only observe or forecast:

- **Script Hub reads live values from the running game's memory.** For example, `BrivUnnaturalHasteHandler.areaSkipChance` and `sprintStacks.stackCount`, for 13 champions. This observes the real handler and needs no model, but it is Windows/AutoHotkey only, needs the game running, and can't evaluate hypothetical formations. [SH]
- **ic_spoilers shows effect data weeks before release.** Example: a Gazrick rework announced for 2027-01-06, with a "prestack" annotation. [SP]

## 2. Classes of hard mechanics and how faithfully each can be modelled

Prevalence counts the 168 available champions (179 `hero_defines` rows) that have at least one effect of the class, with effects attributed via `upgrade_defines`, `hero_feat_defines` and `loot_defines` `effect_def` links. A champion usually falls in several classes.

| # | Class | How it's encoded | Champions | Where the exact rule is | Achievable fidelity |
|---|---|---|---|---|---|
| 1 | **Expression amounts** | `amount_expr`, `per_hero_expr`, `post_process_expr`, … in a small function language: **54 functions**, led by `upgrade_amount` (293 uses), `HasTag` (269), `as_int`, `GetStat`, `GetUpgradeUnlocked`, `floor`, `max`/`min`/`clamp`, `GetFeatEquipped`. Also non-trivial `amount_func` values (`if`, `if_not`, `by_expr`, …; 13 on effects, 120+ in the key catalogue). | 118 | GD (the expression) plus the function semantics, which are inferable from names and usage | **Exact.** Needs a one-off interpreter. A few functions read save stats (class 3) or have side effects (`AppendToSaveStat`, `SetSaveStat`, `BroadcastTrigger`) and belong to class 4. |
| 2 | **Formation- and roster-count stacks** | `stack_func`, 40 values: `per_hero_attribute` (335), `per_crusader` (84), `per_hero` (31), `per_slot`, `per_unique_race`/`_class`/`_role`, `adjacent_champions`, `per_positional_formation_ability`, … | many (part of the 153 with stack fields) | GD plus formation geometry | **Exact** given the formation and the Player Snapshot. A few `stack_func`s are champion-named (`per_ceremorphosis_stacks`, `per_aerois_synergy`, `egbert_atonement`) and fall into class 8. |
| 3 | **Account-state dependent** | `GetStat`/`GetSaveStat`, `per_skin`, `per_feat`, `per_patron_variant_complete`, `per_active_potion`, `per_familiar_in_play`, `num_epics`, `AverageILevels`, `GetLegendaryFeatLevel`, … | 73 | GD plus the Player Snapshot | **Exact where the snapshot exposes the value.** 35 stat names are referenced by effects. **18 of them appear among the 668 `details.stats` keys** (e.g. `kthriss_efficient_bookkeeping`, `jangsao_star_collector_stacks_this_run`). The other 17 (camelCase run stats, per-campaign completions) were not found by name. Otherwise use an assumed value. |
| 4 | **Run-trajectory stacks** | Stacks gained from triggers (kill, hit, area, time): `stacks_on_trigger` (237 keys), `more_triggers`, `expression_on_trigger`, `time_stack_type: time_reset`, `area_change_resets_stacks`, `max_stacks` (196 keys). E.g. Briv Steelbones (per hit taken), Wyll (per kill, reset each area), Tatyana Fury and Rage (per attack received), Ellywick's cards drawn (cap 1,000). | 143 | GD gives the per-stack formula and cap; the stack *count* comes from how the run unfolds | **Formula exact, stack count approximated:** assume `max_stacks`, a steady state, or a stated stack count. Exact only with a combat simulation, which needs class 5. |
| 5 | **Combat-state dependent** | Enemy and party state at runtime: `monsters_on_screen`, `attacking_monsters`, `if_monster_*`, `favored_foe` (49 uses), debuffs and stuns, `hero_health_below`, crits (`default_crit_info`: 2.5% chance, +100%), ultimates (`ultimate_damage_params`: exponential falloff) | 113 | GD gives the rule; the state is runtime | **Approximate:** bounds (min/max) or stated assumptions. Favored foe is close to exact per adventure, because monster tags are in `monster_defines`. |
| 6 | **Random** | `chance` procs (Drizzt, Ulkoria, Prudence, Dungeon Master, Dynaheir, Tess, Cazrin, Viconia), Ellywick's Deck of Many Things (`knight_odds` … `the_fates_odds`, held hand of up to 5 cards), Tatyana's `random_slot`, Môrgæn's random ultimate, D'hani's `paint_odds`, Briv's skip chance | 15 (+ Briv) | GD gives the odds; the rule is in the handler | **Expected value: exact** where the odds are in GD. **Distribution:** Monte-Carlo (byteglow's approach for Briv). A single run can't be exact. |
| 7 | **Real-time / player input** | Daily caps (`cap_increase_per_day`: Diana, Bobby, Eric, Sheila, Hank; Strongheart `additional_tokens_per_day`; Ishi `stacks_per_day`); Wyll `distraction_clicked` (the player clicks distractions) | ~8 | GD gives the parameters | **Not modelled exactly.** The value depends on wall-clock time or player actions. These are mostly gold and chest scavenging, not damage, so they rarely matter to formation strength. |
| 8 | **Champion-unique handlers** | Keys used by exactly one champion: **684 keys**. They are present for **164 of 168** champions; per champion the median is 4, the 90th percentile 8 and the maximum 14 (Hew Maan). Next come Selise (11), then Penelope, Melf, Ellywick and Baldric (10 each). Note #4's "492" counted only keys whose name is prefixed with a champion's name. | 164 | Params in GD; prose in descriptions; **rule in the client handler** (404 champion-named handler classes) | **Exact only by reconstruction:** description text, community maths where it exists (EM for Briv), then validation against in-game tooltip and DPS numbers. Fully exact only from client code (see §1.5 caveats). |

Only **13 champions** have no class 4–7 dependency at all by this heuristic: Jarlaxle, Calliope, Asharra, Makos, Xander, Paultin, Avren, Sisaspia, Dragonbait, Torogar, Talin, Orisha and Gazrick. Only **Calliope** uses nothing but generic keys.

**Worked example (Briv):**

- Effects 567–573: `briv_steelbones`, `hero_dps_mult_per_briv_steelbones`, `briv_unnatural_haste`, `briv_healing_phlo`, `increase_health_by_source_percent`, `briv_unnatural_haste_stack_consume_percent_override`.
- Exact from GD:
  - the per-stack damage buff "behind him" (class 2/4);
  - the Metalborn spec's 3.2%-instead-of-4% stack use;
  - the slot-4 gear `buff_upgrade` amounts.
- Not in GD: how the `buff_upgrade` total turns into skip chance and skip amount. That mapping is EM's reverse-engineered closed form (#4).
- The Steelbones count depends on hits taken during stacking (class 4); Script Hub reads it from memory instead.
- So Briv is **exact with community math plus a stated stack count**, not exact from GD alone.

## 3. How effects stack (what the data states)

- **Within one effect key the order is: base amount → pre-stack modifiers → stacks → post-stack buffs.**
  - `pre_stack`/`pre_stack_amount` keys (153 + 76 uses) add to an ability's base amount before stacking. SP annotates abilities as "prestack".
  - Stacks then apply via `stack_func`. `stacks_multiply: true` (413 keys) gives (1+a)^n; `false` (175) gives 1+n·a.
  - `buff_upgrade`, `buff_buff` and `cap_upgrade` are marked in `effect_key_defines.properties.notes` as **"this is post stack"**. [GD]
- **Across effects:**
  - `amount_func` is `mult` (389) or `add` (192). Every `*_mult` key multiplies damage by ×(1+N/100).
  - Descriptions add the exceptions: "stacking additively" (137), and "stacking additively and then applying multiplicatively" (10; e.g. Tatyana's Fury).
  - Real `_add` keys are rare and mostly crit chance, health or item levels, not damage multipliers. [GD]
- **Caps:**
  - `max_stacks` (196 keys) and "capping at" in text (18).
  - `cap_upgrade` caps an ability.
  - `effect_key_cap` appears on 7 keys (e.g. `monster_health_reduce` 99, mostly trials).
  - `game_rule_defines.time_scale_cap` caps game speed at 10.
- **Exceptions:**
  - `game_rule_defines.not_buffable` lists keys, chains and effect ids that buffs do not affect.
  - `amount_calc_after_bonus` (3 keys) applies the bonus before the amount is computed.
  - `usurp_observe_effect_bonus_blacklist` and `debuff_keys_blacklist` exclude other effects from specific mechanics.
- **Not stated anywhere first-party:**
  - rounding;
  - the order between several post-stack buffs combined with `amount_expr`;
  - how several caps interact;
  - the exact `*_add` bucket semantics.

  These need either the client code or empirical comparison with in-game numbers. Tooltips show computed values; byteglow's formation page is a second check.

## 4. Size of the catalogue and the upkeep

**One-off generic layer (shared by all champions):**

- an interpreter for generic keys;
- the ~40 `targets` forms (#4);
- the 54-function expression language;
- the 40 `stack_func` and ~13 effect-level `amount_func` values;
- the stacking order in §3.

All of this updates itself from GD.

**Champion-adapter catalogue:**

- The upper bound is about **684 single-champion keys**. The client implements them as about **404 champion-named handlers** for 139 champions, plus generic handlers.
- Not all of them affect strength: graphics, underlays, `do_nothing` (173 uses), and survival-only mechanics.
- A strength-relevant subset has not been measured. Key names are too irregular to classify by name: 408 of 684 match no category.
- Expect **roughly 2–8 adapters per champion** (median 4).

**Rate of change** (GD `hero_defines.date_available` and `last_rework_date`, among 168 available champions):

| Year | New champions | Champions reworked |
|---|---|---|
| 2024 | 14 | 8 |
| 2025 | 14 | 11 |
| 2026 to date | 11 | 21 (≈3 per monthly event; e.g. Jim, Omin, Prudence 2026-08-05; Jang Sao, Penelope, Turiel 2026-11-04) |

So in 2026, about **2–3 champions per month** needed new or changed adapters. Behaviour-only fixes (the Ishi example) can change a mechanic with no data diff.

**Change detection is cheap and first-party:**

- `getDefinitions` returns `table_checksums`;
- `effect_defines` can be diffed per champion;
- `changelog_defines` and `last_rework_date` flag reworks;
- SP previews them weeks ahead.

Detection is not the cost; re-deriving each changed rule and validating it is.

## 5. Champions that can't be modelled exactly

- **In principle**, every handler is deterministic code, so nothing is unmodellable given the client code (with the legal and terms-of-service caveat in §1.5).
- **In practice**, from GD, the snapshot and community sources:
  - **No exact value, by nature:** results depend on wall-clock time, player input or RNG state, so even perfect code gives a distribution or an assumption, not a number.
    - Wyll: distraction clicks.
    - Diana, Bobby, Eric, Sheila, Hank, Strongheart and Ishi: daily caps.
    - Ellywick: the hand she holds from the Deck of Many Things.
    - Tatyana: `random_slot`.
    - Môrgæn: random ultimate.
    - D'hani: paint odds.
    - Champions with chance procs.
    - Briv's skip: exact only as an expected value or distribution.
  - **Exact only with a run simulation:** run-trajectory stack builders (class 4, 143 champions). Examples: Briv Steelbones, Tatyana Fury/Rage, Wyll per-kill stacks, Selise, Pwent's Gutbuster stacks ("now only loses half its stacks when changing areas", v652+).
  - **Exact only after per-champion reconstruction:** any champion relying on a class 8 key with no rule in GD, which is 164 champions. The 4 exceptions, with no single-champion keys, are Calliope, Vlahnya, Prudence and Antrius.
- **No champion was found whose mechanic can't be *approximated* with a stated assumption.**

## 6. Options

| Option | Fidelity | Up-front | Upkeep | Notes |
|---|---|---|---|---|
| **A. Reconstruct every handler from description text, community math and validation** | Exact for classes 1–3. Formula-exact plus stated assumptions for 4–7. Exactness of class 8 depends on how good the validation is. | Generic layer plus ~400–700 adapters | ~2–3 champions/month plus behaviour-only fixes | Validation can use in-game tooltips and byteglow's formation page. |
| **B. Derive handlers from the client code** | Highest possible; still assumptions for classes 4–7 | Decompiling plus porting ~400 handlers | Re-diff the assembly every client update | Terms-of-service and legal risk not assessed. The ported code can't live in this public repo. |
| **C. Tiered:** generic layer exact; adapters only for owned or meta-relevant champions; everything else reported as "not modelled" (byteglow's `-1` pattern) | Exact where covered, explicit gaps elsewhere | Generic layer plus tens of adapters | Only for champions the account uses | Doesn't meet "every champion" in v1. Gaps are visible, not hidden. |
| **D. Bounds instead of point values** for classes 4–6 (stacks 0…`max_stacks`, odds as expected value and range) | Honest intervals; rankings may tie | Moderate | Same as A or C | Can be combined with A or C. |
| **E. Observe rather than model** (memory read like Script Hub) | Exact for the *current* run only | High; platform-specific (Windows/AHK today; this client is macOS) | Breaks on memory-layout changes | Can't score hypothetical formations, so it doesn't serve search. |

**Fact relevant to the premise of #10:** classes 4–7 cover about 150 champions. For those, "exact" is reachable only as an *exact formula under a stated assumption* (stack count, enemy state, expected value), whatever source is used.

## Open uncertainties

- Classification counts come from keyword heuristics (±10%), and effect attribution follows `effect_def` links only (1,471 of 3,089 effects attributed to champions; the rest are monster, area, patron and other effects).
- The meaning of 17 referenced stat names, and where Briv's Steelbones/Sprint counts sit in the snapshot, was not located. No key name contains "briv", "sprint" or "steelbone".
- The client code was not read beyond type names. Whether description text matches handler behaviour was not checked anywhere.
- The wiki is still unreachable (HTTP 402).
