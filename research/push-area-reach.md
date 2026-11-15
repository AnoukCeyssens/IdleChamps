# How does a Push formation's strength translate into the highest area it can reach?

Research for issue #14. Question: how does the Main DPS's damage, with all its buffs, map to a reachable area? The Push tab must be able to say whether a recommended formation is expected to reach an adventure's objective area. That includes variant and patron objectives such as 250–825. This document covers:
- how monster health and armour scale per area;
- how BUD relates to damage;
- what the community uses to find a wall;
- how the `highest_area_completed_ever_c<adventureId>` stats can calibrate an estimate.

It ends with options for v1, each with its accuracy and cost. It does not pick one.

Date: 2026-11-15. Vocabulary follows `GLOSSARY.md`. This builds on `research/formation-strength-model` (strength ≈ Main DPS × the product of buffs reaching its seat) and `research/game-api-and-data-shape`.

## Sources

| ID | Source | Trust |
|----|--------|-------|
| GD | The game's cached `getDefinitions` payload, `downloaded_files/cached_definitions.json` (21 MB). Tables read: `game_rule_defines` (rule `monster_base_stats` and others), `campaign_defines[].game_changes`, `adventure_defines_*[].game_changes`, `monster_defines`, `patron_defines`, `effect_key_defines`, `hero_defines`, `attack_defines`. | Primary |
| UD | The `getuserdetails` response in `webRequestLog.txt`. Read: its `defines` block (`adventure_area_defines`, `quest_defines`, `monster_defines`, `adventure_defines` for the active campaigns) and numeric `stats` / `game_instances[]` fields. No credential or identity values were read or copied. | Primary (one account, one moment) |
| BG | ic.byteglow.com production bundles (`assets/shared-*.js` and the adventure, formation, goals, stats, summary, speed and variants chunks), fetched 2026-11-15 and searched as minified text. | Primary for how that tool works |
| SC | Steam discussion "BUD dmg doesn't follow enemies HP", 2022-05-18, steamcommunity.com/app/627690/discussions/0/3282573132380248656 | Anecdote |
| WK | Wiki (idlechampions.fandom.com) and Reddit. Web searches turned up no monster health formula or wall calculator, and the wiki blocks automated fetches (see the strength-model research). Nothing is cited from them. | n/a |

All numbers were computed with throwaway `python -I` scripts over GD and UD on 2026-11-15. They will drift with game updates.

## 1. Monster health per area is pure data

### 1.1 The global curve

`game_rule_defines` rule `monster_base_stats` [GD]:

| Field | Value | Meaning (inferred where marked) |
|---|---|---|
| `base_health` | 10 | Health of a normal monster at area 1 |
| `health_growth_rate_curve` | `{1: 2.031, 2001: 3.031, 2251: 4.531}` | Per-area multiplier, piecewise by area |
| `health_gold_ratio_curve` | 0.65 at area 1, falling to about 0.33 by area 202 and about 0.32 by area 2481 | Gold drop ≈ health^ratio (inferred, see §1.4) |
| `base_dps`, `dps_growth_rate_curve` | 1; ×1.75 at areas 50, 100, 151, 201, …; ×4 from 2001; ×1e10 at 2451 | Monster damage to champions |
| `power_boost_time` / `_growth_rate` | 10 / 1, additive | Monsters hit harder the longer they live (inferred) |

Other relevant rules: `max_area` is 2501 and `max_modron_auto_reset_area` is 2500 [GD].

Reading of the curve (inferred: the client code that applies it is not visible): a normal monster's health is

```
HP(a) = base_health × ∏_{k=2..a} g(k) × monster.health_modifier × adventure modifiers
log10 HP(a) = log10(base_health) + Σ_{k=2..a} log10 g(k) + …
```

`g(k)` is the curve value in force at area k. One assumption could not be checked: whether the multiplier starts at area 1 or area 2 shifts everything by one area.

### 1.2 Campaigns override the curve

`campaign_defines[].game_changes` holds a `monster_base_stats` override for 21 of 28 campaigns [GD]:

| Campaign(s) | base_health | growth per area | Areas gained per ×10 damage |
|---|---|---|---|
| Grand Tour, Tomb of Annihilation, Dragon Heist, Witchlight, Xaryxis, Fortune's Wheel, Tales of the Champions (no override) | 10 | 2.031 → 3.031 at 2001 → 4.531 at 2251 | 3.25 → 2.08 → 1.52 |
| All 18 event campaigns (Highharvestide … Brightswords, Time Gates) | 10 | 1.85, flat | 3.74 |
| Descent into Avernus | 20 | 2.035 → 3.035 → 4.535 | 3.24 |
| Rime of the Frostmaiden | 40 | 2.04 → 3.04 → 4.54 | 3.22 |
| Vecna: Eve of Ruin | 40 | 2.31 → 3.31 → 4.81 | 2.75 |

This gives the size of normal and boss health on those curves (log10, boss = ×50):

| Area | Core 2.031: normal / boss | Event 1.85: normal / boss | Vecna 2.31: normal / boss |
|---|---|---|---|
| 50 | e16.1 / e17.8 | e14.1 / e15.8 | e19.4 / e21.1 |
| 100 | e31.5 / e33.2 | e27.5 / e29.1 | e37.6 / e39.3 |
| 250 | e77.6 / e79.3 | e67.5 / e69.2 | e92.1 / e93.8 |
| 500 | e154.5 / e156.2 | e134.3 / e136.0 | e183.0 / e184.7 |
| 1000 | e308.4 / e310.1 | e267.9 / e269.6 | e364.9 / e366.5 |
| 2000 | e616.1 / e617.8 | e535.1 / e536.8 | e728.5 / e730.2 |
| 2500 | e900.6 / e902.3 | e668.7 / e670.4 | e1029 / e1031 |

**The key consequence: area reach is logarithmic in damage.** On core campaigns, each ×10 of sustained damage buys about 3.25 areas. So a modelling error of a factor of 100 costs only about 6.5 areas, and a 30-area margin above an objective tolerates about a ×1e9 error. This is what makes an "expected to reach area N" estimate feasible at all, given how much of the strength model is approximate (issue #10).

### 1.3 Per-monster and per-adventure modifiers

- **Bosses.** In the sampled sets, `monster_defines.type` 2 (bosses) have `health_modifier: 50`; most normal monsters have none, so ×1 [UD]. The cached table has more values (4, 5, 10, 20, 25, 30, …), used by special monsters [GD]. Every 5th area is a boss area: three waves and a boss (`adventure_area_defines[].waves`, e.g. area 50 = `[[…5 trash], [...5 trash], [boss 155]]`) [UD]. Other areas need 25 kills (`quest_defines.goal_amount: 25`) [UD]. Area sets are 50 areas long and loop [UD].
- **Adventure and variant `game_changes`.** Of 1,993 adventure defines, 97 carry health-affecting changes [GD]:
  - `global_effects` (28) and `add_monster_properties(_by_tag/_by_id)` (32), e.g. `{lycanthrope: {health_multiplier: 20, damage_multiplier: 5}}`;
  - `effect_strength_by_area` (3 that touch health), e.g. "It's Getting Darker": `monster_health_increase,replace` with base 8, growth 7.258 added per area;
  - `monster_level_offsets` (3, "Split The Party": `level_mult` 4/8/11);
  - `set_monster_health_type: armor_based` (4, see below);
  - `random_monster(_waves)`, `guaranteed_monster(s)`, `additional_bosses`.

  `effect_strength_by_area` also scales player buffs per area (e.g. `global_dps_multiplier_mult` ×40 per area in "Catti-alone"). These are all structured, but each type needs its own small handler. The `effect_key_defines` catalogue has about 88 `area`-owner keys that change monster health or damage taken (`monster_health_increase`, `monster_with_tag_health_increase`, `increase_monster_damage*`, `monster_mitigation`, …) [GD].
- **Patrons change no monster stats.** All five `patron_defines[].game_changes` are champion restrictions only (Mirt: good or evil; Vajra: CON ≥ 14; Strahd: INT ≥ 13; Zariel: STR ≥ 10 and CHA ≥ 13; Elminster: released or reworked in the last 3 years) [GD]. A patron objective (250–825) differs from the base objective only in *which champions are allowed* and in the target area. The health curve is the campaign's.
- **Armour.** `set_monster_health_type: armor_based` with an `amount` (4 or 25), and monsters with `properties.hits_based_damage` (Shadowy Wall, Strange Orb, …), die after a number of *hits*, not an amount of damage [GD]. Keys such as `monster_armor_increase`, `decrease_monster_armor_health_per_point_diff` and `bonus_armored_damage` exist [GD]. Armour does not scale with damage. It is a hit-count or ability-score check, so it is outside a damage-based estimate. These adventures are few (4 adventure-wide, plus special monsters) and should be flagged rather than modelled.

### 1.4 Consistency checks against the sample account

The game client's code that applies these values was not visible (see issue #10), so the formula was checked against the one account in UD:

- **Instance 2**: Pure Energy, campaign 1 (core curve), currently at area 85 = its highest. Measured `dps` 2.35e26 and BUD (`this_reset_highest_damage_dealt`) 4.27e27. Predicted HP: normal e26.85 (about 3 s per kill at that DPS) and boss e28.55 (about 150 s per kill). This fits a run sitting just short of its objective at area 100.
- **Gold**: instance 1 (Feast of the Moon, event curve 1.85, area 419). Predicted log10 HP = 112.7, and HP^ratio (0.332) ≈ e37.5. The account's `highest_gold_hoarded` is 2.39e41: about e4 of gold multipliers and accumulation on top. Under the core curve the same check would give e43, and a linear gold ratio would give e112. This supports both the 1.85 event override and the "gold ≈ HP^ratio" reading.
- **Instance 1's damage**: its `dps` of 1.6e124 is about 1e11 above normal HP at area 419. The model therefore says this run was *not* walled at 419: its DPS wall would be around 455 for bosses. The snapshot does not show why the run was at 419. Possibly it was simply parked or progressing, since `current_area` and `highest_area` move together. Without a second snapshot this cannot be confirmed either way.
- `highest_dps_ever` of 1.2e201 compares with the account's best core-curve stats (`c1907` = 623, `c1862` = 567): boss HP at 623 is e194, so the DPS is above the boss HP the account has passed. This is consistent, though it does not prove anything.

So the curve shape is very likely right. The exact offset and rounding are unverified.

## 2. BUD and damage

- **BUD.** The highest single hit a champion has dealt recently, which the game uses to scale ultimates. The glossary's Main DPS is the "BUD-setting" champion. The snapshot exposes it per game instance as `stats.this_reset_highest_damage_dealt` (and `this_reset_highest_base_damage_dealt`), with `this_reset_highest_damage_dealt_hero_id` naming the champion [UD].
- **Measured damage is in the snapshot.** Per instance, the snapshot also has:
  - `stats.dps` (current formation DPS);
  - `global_dps_multiplier`;
  - `champion_<id>_base_damage_this_reset` and `champion_<id>_ultimate_damage_this_reset`;
  - account-level `highest_dps_ever` [UD].

  This gives v1 a measured anchor (§4.C) without simulating anything.
- **Hit versus DPS depends on the champion.** `attack_defines` gives `cooldown`, `num_targets` and `damage_modifier` [GD]. In the sample:
  - Kas (153, cooldown 5 s, 1 target): BUD 1.53e124 against instance DPS 1.64e124;
  - Asharra (6, cooldown 7 s, 3 random targets): BUD 4.27e27 against DPS 2.35e26, a ratio of about 18.

  So BUD ≈ the Main DPS's per-hit damage and DPS ≈ BUD × hits/s × targets ± other champions. On a log scale the gap is 1–1.5 orders of magnitude, which is about 3–5 areas.
- **Ultimates.** Rule `ultimate_damage_params`: `dps_based: true`, `min_highest_hit_multiplier: 0.01`, exponential falloff with `falloff_delay` and `falloff_period` 15 s [GD]. Ultimates are derived from BUD and usually don't move a wall. Exceptions are champions whose ultimates overkill BUD by a lot: SC reports "up to e30 over BUD" for Penelope and Blooshi, an anecdote. These belong in the per-champion handlers, not the generic estimate.
- **Wall definition used here.** An area is a wall when the formation can no longer kill its monsters within a reasonable time. In practice the boss (×50 HP) is the binding check every 5 areas. Monster damage grows only ×1.75 every 50 areas (plus a time-based power boost) while health grows about ×2 per area. So past the early game, walls are HP/DPS walls, not survival walls (inferred from the curves). Survival can still matter in restricted variants without tanks or healers.
- **Champion levels aren't free during a run.** Gold ≈ HP^0.33…0.65 [GD, inferred], so levels are bought as the run progresses. For an established account the Main DPS reaches its last damage upgrade long before its wall, so the "fully levelled" damage is the right input. The `champion_<id>_highest_level` stats help decide this [UD]. For champions not yet levelled that far, the wall comes earlier. This is a Scoring Assumption to state, not something v1 should simulate.

## 3. What the community does

- **No wall calculator was found.**
  - byteglow's bundles contain no monster-health code. Searching every downloaded chunk for `health_growth`, `monster_base_stats`, `base_health` or `growth_rate` found nothing [BG].
  - byteglow uses `highest_area_completed_ever_c${adventure_id}` only to display per-adventure progress, and to take a maximum over a campaign's adventures for goals [BG].
  - Emmotes' route and spoiler tools handle Briv routes, not walls (strength-model research).
  - Web searches for a monster HP formula or a "BUD to area" calculator came up empty.
- **The community practice is empirical.**
  - Push until progress stalls, then compare BUD to enemy HP ("BUD 195e and enemies with over 220e HP" [SC]).
  - Spike BUD (Artemis, Alyndra), use wall-breaking abilities (Shaka) or big-overkill ultimates [SC].
  - Use the Modron area goal to stop just before the wall.

  In other words, players read the wall off the game, which is exactly the data the snapshot already carries.

## 4. How the player's stats can calibrate an estimate

- **`highest_area_completed_ever_c<adventureId>`** is keyed by **adventure id**, not campaign id. Evidence: `c2036` = 419 for the running instance on adventure 2036 [UD], and byteglow indexes it by `adventure_id` [BG]. There is one value per adventure. Variants have their own adventure ids, so they get their own stat. **Patron runs share the base adventure's stat.** Patron objectives live in the same define's `patron_objectives`, and no patron dimension exists in the key. The sample account has 32 such stats [UD].
  - It is a **floor**: the account once reached area N here, possibly with a formation the current recommendation can't use (another patron, champions since benched).
  - It carries **no strength**: there is no BUD or formation from that run. So alone it cannot calibrate the damage→area mapping; it can only confirm "already reached".
  - Old adventures may hold stale or low values. For example, `c1` = 25 although its objective is 50; the account may have completed it before the stat existed.
- **Live instances are the real calibration points.** Each `game_instances[]` entry gives:
  - `current_adventure_id`, `current_patron_id`, `current_area` and `highest_area`;
  - the live formation (`formation`, `hero_in_seats`);
  - measured `dps`, BUD and its champion.

  Each instance is one (formation, adventure, damage, area) observation. Reading the predicted HP at that area against the measured damage gives the account's "kill-time tolerance" T, with a correction for model bias. That only works if the run is at its wall. A single snapshot can't tell walled from parked (instance 1 above). Two snapshots some minutes apart, with the same area and an unchanged formation, can. That depends on the map's open "Snapshot history" item.
- **Deprecated and absent stats**: rule `deprecated_stats` marks `highest_freeplay_area_by_campaign` read-only [GD]. byteglow also reads `*free_play_highest_area` keys [BG], which this account doesn't have.

## 5. Options for v1

All options can show the per-adventure floor ("reached N here before") for free. They differ in how they turn the recommended formation's strength into an area.

### A. Floor only: no damage model

The verdict is "Reached" if `highest_area_completed_ever_c<adv>` ≥ objective (non-patron objectives only), otherwise "Unknown".

- **Accuracy**: exact when it says yes. It says nothing for new adventures, new variants or any patron objective, and those are exactly the cases where Push advice matters.
- **Cost**: trivial.

### B. Absolute model: computed damage against the HP curve

Compute the Main DPS's absolute fully-levelled damage. The ingredients are:
- `base_damage`, upgrade multipliers up to its reachable level, gear, feats, legendaries;
- the buff product from the strength model;
- the attack cooldown and targets.

Then solve `HP_boss(a) ≤ damage × T` on the target adventure's curve (§1.2), with its adventure modifiers. Report a for the boss areas.

- **Accuracy**: area error ≈ 3.25 × log10(model error factor). Opaque champion effects are typically worth 1e2–1e10, which gives ±6–30 areas. Unmodelled BUD spikers or ultimates bias the estimate low. The method also needs T, which can't be calibrated without B's own absolute numbers being right.
- **Cost**: high. It needs a complete *absolute* damage model, including level costs and the "last upgrade reached" assumption. Option A in the strength model only needs *relative* strength between formations.

### C. Anchored relative estimate: measured anchor × modelled ratio

Take a live instance as the anchor: its measured damage D₀ (BUD or `dps`) and its formation F₀. For a recommended formation F on target adventure X:

```
D_F  ≈ D₀ × S(F) / S(F₀)      # S = the strength model's relative score; shared unknowns cancel
reach(X, F) = max a : log10 HP_X,boss(a) ≤ log10(D_F × T)
```

T comes from the anchor at its wall if known; otherwise a default (e.g. a boss kill within about 30–60 s, a stated Scoring Assumption).

- **Accuracy**: errors only come from what *differs* between F and F₀, plus T. The account-specific constants cancel: gear, levels, the base-damage model. A ×10 error in the ratio is about 3 areas. Realistically ±5–15 areas where the anchor and the recommendation share a Main DPS, and worse when they don't.
- **Weaknesses**: the anchor may not be at its wall, or may not be fully levelled. That makes it underestimate D₀, a safe-side bias. It fails when no instance runs a comparable formation.
- **Cost**: medium. It reuses the strength model, the HP curve (a handful of data fields) and about 10 adventure-modifier handlers. Armour-based adventures are flagged "not estimated".

### D. Banded verdict (C or B, plus A, shown as a band)

Show "Expected to reach ~N (likely / borderline / unlikely to reach objective M)". The band is margin = N − M against an uncertainty width, e.g. ±10 areas core and ±12 event, wider when unmodelled effects are present. A "Reached before" floor from A overrides when it applies.

- **Accuracy**: honest about the uncertainty, and matches how the Push tab is read: the decision is "go / not yet", not the exact area.
- **Cost**: C's cost plus a little UI. It fits the "Scoring Assumption" pattern already in the glossary.

### E. Learn from snapshot history

Once Player Snapshots are kept, record (adventure, formation strength S, area where progress stopped) whenever a run stalls. Fit T and a per-account bias of the model, so C and D tighten over time.

- **Accuracy**: improves with use. The first weeks match C.
- **Cost**: medium, and it depends on the map's open "Snapshot history" decision. It is a natural follow-up rather than v1.

### Cross-cutting notes for the decision

- The HP side is *cheap and exact enough*: about 40 lines covering the rule, the campaign overrides and a few adventure modifier types. The damage side carries all the uncertainty, and the logarithm forgives most of it.
- For patron objectives (250–825) only champion eligibility differs, so they need no extra HP handling. But their stat floor is shared with the base adventure, so A can't answer them.
- Areas 2001+ switch to growth 3.031 / 4.531, so each ×10 buys only about 2 or 1.5 areas. v1 objectives (≤ 825) never get there.
- Hits-based and armour monsters, `monster_level_offsets`, timers and escort adventures should surface as "estimate not available: <reason>" rather than a number.

## Open uncertainties

- The client code that applies `health_growth_rate_curve` (multiplier start index, rounding, how `health_modifier` and adventure `health_multiplier` combine) was not visible. The formula is inferred and checked only against one account.
- The exact meaning of `power_boost_*` (monster damage ramp over time) and of `this_reset_highest_damage_dealt`'s time window is not documented in the data.
- Whether a snapshot instance is "at its wall" cannot be told from a single snapshot.
- No community source states a kill-time tolerance T. Any default is an assumption to show the player.
