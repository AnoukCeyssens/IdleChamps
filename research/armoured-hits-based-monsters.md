# How do armoured and hits-based monsters work, and what does a formation need to beat them?

Research for issue #17. Question: how do armour-based and hits-based monsters work, which adventures use them, which champion attributes help, how the community handles them, and how could the Push estimate and the formation search account for them instead of only flagging them? It ends with options and their accuracy and cost. It does not pick one.

Date: 2026-11-15. Vocabulary follows `GLOSSARY.md`. This builds on `research/push-area-reach` (monster HP curve; reach is logarithmic in damage, about 3.25 areas per ×10 on core campaigns) and `research/formation-strength-model` (strength ≈ Main DPS × product of buffs reaching its seat).

## Sources

| ID | Source | Trust |
|----|--------|-------|
| GD | The game's cached `getDefinitions` payload, `downloaded_files/cached_definitions.json` (21 MB). Tables read: `monster_defines`, `adventure_defines[].game_changes` and `restrictions_text`, `attack_defines`, `effect_defines_*`, `effect_key_defines`, `upgrade_defines_*`, `hero_defines`, `game_rule_defines`, `text_defines`, `tutorial_state_defines`, `patron_defines`. Contains no account data. | Primary |
| UD | Definition tables only (`adventure_area_defines`, `quest_defines`, `monster_defines`) extracted from the `getuserdetails` response in `webRequestLog.txt` by a script that keeps only `*_defines` lists and drops any credential-like field. No credential or account value was read or copied. | Primary (active campaigns only) |
| TU | `tutorial_state_defines` 94 and 95 in GD: the game's own in-game explanation of the two health types. | Primary (first-party text) |
| S1 | Steam discussion "Armored units and ults", 2020-06-04/05, steamcommunity.com/app/627690/discussions/0/2298472007787319813 | Anecdote |
| S2 | Steam discussion "What does the shield icon on a boss mean?", 2020-10-15/16, steamcommunity.com/app/627690/discussions/0/5118863332768135681 | Anecdote |
| S3 | Steam discussion "WHat are good heroes to deal with armored bosses?", 2022-05-18/20, steamcommunity.com/app/627690/discussions/0/3282573132380248656 | Anecdote |
| WK | Wiki `idlechampions.fandom.com/wiki/Favored_Foes`, section "Armored/Hit-Based and Other Damage Quick Reference Table". HTTP 402 to fetches; only its search-engine snippet was seen ("To destroy an armored health chunk, Champions need to meet or beat a certain BUD level of damage (that increases the further you progress in an adventure)"; table rows Ezmerelda, Grimm, Binwin, Hew Maan "only bosses", Minthara, Windfall, Gazrick, Lucius). | Secondary, partial |
| ES | emmotes.github.io/ic_spoilers (Binwin page, via search snippet) | Secondary, mirrors GD text |

No Reddit thread or Codename blog post on the mechanic was found. byteglow has no monster-health code at all (push-area-reach research), so no community tool models these monsters. Counts were computed with throwaway `python -I` scripts over GD on 2026-11-15 and will drift with game updates.

## 1. How the two health types work

The game's own tutorial text is the most authoritative statement [TU]:

- **Hits-based** (`tutorial 94`): "It's got hit-based health. Any Champion who strikes it will deal one hit, no matter how much damage they actually do."
- **Armored** (`tutorial 95`): "You need to deal a certain amount of damage in a single hit to take off a chunk of armor, and it's got a lot of chunks! Ultimate abilities are a great way to deal with these types of enemies."

The UI calls them "Hits-Based", "Armored" and "Unarmored" (`text_defines` 5190, 5193, 5194) [GD]. Recent effect text also calls both "segmented health" (Flint, Kitiara, Lucius, Clockwork Clash) [GD].

### 1.1 Hits-based

- **Damage does not matter.** Each successful hit removes one segment [TU, S2].
- **What counts as a hit**: each target an attack strikes. An attack with `num_targets` 3 or `projectile_count` 3 spread over targets gives each target one hit. Several attacks carry `unique_hit_multiplier` 2 or 3 (Mehen, Selise, Tatyana, Rust), meaning that attack counts 2–3 hits on one target [GD `attack_defines` 393, 398, 485, 499, 500, 782; long_description "This can cause hits- and armor-based enemies to take two hits"]. DoT ticks count as hits (community; S1, S2, S3 on Donaar, Krull, Torogar, Jaheira).
- **Count of hits** = the monster's `health_modifier`, or `override_health_multiplier` when an adventure sets it (inferred: restriction texts say "25 armored health", "4 armored health" where the data has `amount: 25`/`4`; S2 "the number of segments in the bar is how many hits it has remaining … for big bosses it's usually 50"). Hits-based bosses are mostly `health_modifier` 50 (114 of 157); hits-based trash are mostly static objects (barriers, portals, Strange Orb, `static`/`spawner` tags) with 4–50 hits [GD].
- **Area does not change the hit count**, so a hits-based monster is never a damage wall. It can be a *time* wall (monster power grows the longer it lives, see `power_boost_*` in push-area-reach §1.1) or a survival wall. Some hits-based enemies regenerate [S2].
- **Clicks**: not confirmed whether clicks count as hits. Rule `click_damage_settings` grows click power by 2.031 per level, the same factor as monster health [GD], and "Burden of Legacy" says "click damage has no effect after area 100", implying clicks normally matter there. Treat clicking (and familiars that click) as an unverified Scoring Assumption.

### 1.2 Armored

- **One chunk per qualifying hit.** A hit removes a chunk only if its damage meets the per-chunk threshold; below the threshold it does nothing; above it the excess is lost [TU, S1 Zrazzt, S2 Soteria, WK]. One 2020 post (S1 ptor) claimed `floor(damage / chunkHP)` chunks per hit, but other posts in the same and a later thread report "BUD e150 still takes 1 segment per hit" and "a single attack can only destroy one segment per hit" [S1, S2]. The data agrees with "one per hit": every armour-breaking effect is phrased as "+1 extra segment", "double", "3 times" per successful hit (§3), which would be meaningless if big hits already took many chunks.
- **Number of chunks** = `health_modifier` / `override_health_multiplier` / `armor_chunks` / `set_monster_health_type.amount` (§2). 175 of 213 armored bosses have 50 [GD].
- **Threshold per chunk** ≈ the monster's max HP ÷ number of chunks (S2: "dividing [max HP] by about 50 gives the damage threshold per hit"; S1 "1 segment of e14"). For an armored boss with `health_modifier` 50 this is HP_boss(a)/50 = **the HP of a normal monster at that area** (inferred from the HP model in push-area-reach). The threshold therefore grows with the area curve like everything else (WK: "increases the further you progress").
- **Modifiers on the threshold or chunks**: `increase_armor_section_health` / `reduce_armor_section_health` (Armored Assault: "Armored enemies require 100% more damage to deal one armored hit"), `monster_armor_increase(_add)` (more chunks; Durable Dragon's Chotyn gains +5% per respawn), `decrease_monster_armor_health_per_point_diff` (threshold scaled by a champion ability score), `override_armor_based_min_health_multiplier: 25` (at least 25 chunks) [GD].
- **Armour layer before normal health**: `first_health_ranges: [{type: armor_based, armor_chunks: N}]` gives N chunks that must be broken first, then a normal damage-based bar (Lolth's Domain, Prisoners of Lolth, Chipping Away, Burden of Legacy) [GD]. In Burden of Legacy a knockback removes that layer (`pushback_removes`).
- **Ultimates** scale from BUD (rule `ultimate_damage_params`, `dps_based`) with small `damage_modifier`s (0.006–0.04) [GD]. A single-hit ult takes one chunk at most; ults with many ticks or projectiles (Donaar, Krull, Torogar: 30 s pools ticking every 0.6 s = 50 ticks) can strip a whole 50-chunk boss *if each tick meets the threshold* [GD attack 140, 251, 329; S1, S2]. Overkill ults (Penelope, Blooshi: "up to e30 over BUD", S3) are the ones whose ticks clear thresholds at high areas.
- **Armored + hits on one monster**: only 1 monster carries both flags [GD]; "armored hit-based health" in restriction text means armored.

### 1.3 What that means for a wall

On the core curve (2.031 per area), a 50-chunk armored boss needs per-hit damage ≥ HP_normal(a), which is 1/50 of the boss HP: log10 50 ≈ 1.7 orders, about **5.5 areas easier on the damage axis** than killing a normal boss in one hit. But it needs **50 qualifying hits**, and usually only the Main DPS's hits qualify (support champions hit many orders below BUD). With a 5 s cooldown and one target, that is about 250 s per armored boss, versus a few seconds of sustained DPS on a normal boss. So the armored wall is:

```
qualifies(a)   = BUD_or_per_hit_damage ≥ chunkHP(a)                          # damage check, log-scale like push-area-reach
kill_time(a)   = chunks / Σ_champions_that_qualify(hits_per_second × chunks_per_hit)   # rate check
wall           = first armored-boss area where not qualifies(a) or kill_time(a) > T_armor
```

Hits-based: `kill_time = hits / Σ_all_champions(hits_per_second × hits_per_attack)`, no damage term at all.

## 2. Where they appear and how to detect them

### 2.1 Monster-level flags

`monster_defines` (2,372 rows) [GD]:

| Flag | Count | Notes |
|---|---|---|
| `properties.armor_based_damage: true` or tag `armor_based` | 272 (213 bosses `type` 2, 59 trash) | tag and property disagree for 9 newer monsters (ids 961, 973, 2201, 2215, 2219, 2339–2341, 2354): check both |
| `properties.hits_based_damage: true` or tag `hits_based` | 286 (156 bosses, 129 trash, 1 both) | trash are mostly `static` objects |

Each monster lists the `adventures` it appears in. Through that list, **358 of 1,993 adventures contain at least one armored or hits-based monster, 303 of them a boss** [GD]. These are mostly the story bosses: e.g. area set 12 (Grand Tour) has the hits-based "Resurrected Hero of the Vale" as its area-20 boss, which recurs at 70, 120, … because area sets loop every 50 [UD]. Every campaign is affected: 40 Grand Tour adventures, 20 each in the big campaigns, 12–14 per event [GD].

**This corrects push-area-reach §1.3**, which saw only the 4 `set_monster_health_type` adventures. Armoured/hits-based monsters are not a rare edge case: they are a recurring boss in about one adventure in five, so a Push estimate past their area must deal with them.

### 2.2 Adventure and variant `game_changes`

35 adventures (all variants) add or change armour through `game_changes` [GD]:

| `type` | Effect | Examples |
|---|---|---|
| `set_monster_health_type` `{health_type: armor_based, amount, target_class, target_tags}` | make bosses/trash/tagged monsters armored with N chunks | Breaker Breaker (bosses 25), A Specific Species (bosses 25), Clockwork Clash (non-boss constructs 4), Way Under (trash 4) |
| `add_monster_properties(_by_tag/_by_id)` with `armor_based_damage` + `override_health_multiplier` | every / some monsters armored | Durable Deep (all: trash 5, bosses 50), Veteran Brigade (bosses 50), Shattered Shields (16% of monsters, 5), Armored Assault (bosses ≥25, +100% threshold) |
| same, with `first_health_ranges` `armor_chunks` | armour layer before normal health | Lolth's Domain / Prisoners of Lolth (drow 4, bosses 8), Elves Are Wild, Unlucky Hunters, Chipping Away (all 4), Burden of Legacy (10 after area 10) |
| `guaranteed_monster(s)` / `random_monster(_waves)` with `armor_based_damage` / `hits_based_damage` | extra armored or hits-based monsters, often `protects_other_monsters` (must die first) | Distracted Escape, Designation: Hostile, Vault of Cuteness, Musical Accompaniment, New Moon Nemesis, Half-Full Outlook (hits, dies to specific attack ids) |
| `override_armor_based_min_health_multiplier` | armored bosses have at least N chunks | Grimm's Icebreaker, The Ruinous Bosses (25) |
| effect `increase_damage_against_monster_armor_and_hits,3` for tag `aarakocra` | variant-granted breaker | The Ruinous Bosses |

Some variants instead exempt these monsters from a rule (`by_tag: "!hits_based^!armor_based"` in Strike While It's Hot, Tempered Foes), so the tag must be honoured when applying other adventure modifiers too.

### 2.3 Patrons

`patron_defines` contain no armour or hits data [GD]. Patron runs share the base adventure's monsters, so a patron objective (250–825) meets the same armored/hits bosses as the base run, just deeper, which makes them more likely to be the binding wall.

### 2.4 Detection recipe

1. Per adventure: union of (monsters whose `adventures` include it) and (`game_changes` of the types above).
2. Per area: the waves in `adventure_area_defines` say which monster spawns at which area. These are **not in `cached_definitions.json`**; they come with `getuserdetails` (`defines.adventure_area_defines`) for active campaigns only [UD]. For other adventures, only "somewhere in this adventure" is known unless area defines are fetched per campaign.
3. Per monster: chunks = adventure override → `armor_chunks`/`amount` → `health_modifier`; apply `monster_armor_increase*` and `*_armor_section_health` modifiers.

## 3. Which champion attributes help

### 3.1 Generic data (any champion, read from `attack_defines`)

- **Attack rate**: `cooldown` (base attack). Faster attacks = more hits. Generic `reduce_attack_cooldown`-style buffs and Hew Maan's Yassa reduce it.
- **Hits per attack**: `num_targets`, `projectile_count` (Asharra, Farideh, Jim, Ulkoria, Orisha, Ellywick, Nordom, Virgil …), `projectile_multi_hits`, `unique_hit_multiplier` (Mehen, Selise, Tatyana, Rust). Against a single boss only hits that can land on the same target count; random multi-target attacks spread when trash is alive.
- **Ultimates with many hits**: `damage_over_time`, `pool_time`/`pool_interval` or `line_time`/`line_interval` (Donaar, Krull, Torogar: 50 ticks), `projectile_count` / `num_targets` (Catti-brie 6, Dhadius, Birdsong, Stoki …), `projectile_multi_hits` (Celeste, Jarlaxle, Corazón). 39 of 168 ults use a custom animation `type` (`donaar_ultimate`, `krull_ultimate`, …) whose tick fields are readable but whose exact behaviour is in client code [GD].
- **Per-hit damage**: only matters for armour. It is BUD for the Main DPS; for other champions it is their own per-hit damage, usually far below BUD, so they rarely qualify.
- **Knockback / control**: a knockback removes the armour layer in Burden of Legacy (`pushback_removes`); keeping bosses away keeps DoT pools effective (S3). Control role tags (`control_knockback`, `control_stun`) are generic [GD `role_tags_v2`].

### 3.2 Breaker effects (champion-specific)

The game has a role tag `breaking` ("Breaker") in `role_tags_v2` [GD]. Champions tagged `breaking` in `hero_defines`: **Binwin, Wulfgar, Lucius, Hew Maan, Selise, Gazrick, Minthara, Grimm, Kitiara**; gained by upgrade: **Ezmerelda** (lvl 200), **Windfall** (100), **Skylla** (200), **Flint** (200) [GD].

| Champion | Effect (GD text) | Effect key | Generic? |
|---|---|---|---|
| Hew Maan (Kleeb, middle columns) | boss attacks destroy twice as many segments; passive: hits-based enemies 50% chance to spawn with some hits gone | `unique_hit_multiplier` via `hewmaan_*` | position-dependent custom |
| Ezmerelda | all champions +% damage vs armored; marked enemies lose an extra chunk per hit (her column) | `monster_with_tag_more_damage,…,armor_based`, `increase_damage_against_monster_armor,1`, `…_hits,1` | generic keys, conditioned on her debuff |
| Selise | Shield of Psychomancy: armored/hits enemies take ×2 hits (×3 with Relentless Avenger) from **all** attacks | `unique_hit_multiplier,100/200` debuff | generic key, custom trigger |
| Binwin | double hits vs hits/armored; Resolve stacks when he fails to break armour | `increase_num_unique_hits`, `hero_dps_multiplier_mult` stacks | generic-ish |
| Grimm | +1 chunk per break, stacking to +5, reset per area | `increase_damage_against_monster_armor_and_hits` with stacks on `monster_armor_segment_broken_by_owner` | generic key + trigger |
| Flint | champions with Forged Bonds remove +1 segment | `increase_damage_against_monster_armor_and_hits,1` filtered by `HasEffectByID` | generic key + filter |
| Kitiara | Merciless Resolve champions break (max − n affected) extra | `increase_damage_against_monster_armor_and_hits` | generic key, custom amount |
| Minthara | ≥2 Soul Branding stacks: +1 chunk/hit | `increase_damage_against_monster_armor/hits,1` | conditional on stacks |
| Windfall | prefers armored/hits targets; +1 piece per hit | `increase_damage_on_armor_and_hits` | generic key |
| Skylla | Faerie Fire targets lose +1 piece when ≥1 broken | `increase_damage_on_armor_and_hits` | generic key |
| Lucius | Acid Splash removes +1/+2 extra segments; acid doubles armour loss | `lucius_armor_eating_acid`, `unique_hit_multiplier_from_attacks` | custom |
| Gazrick | cold attacks lower the armour threshold by X%, stacking | `gazrick_armor_ablation(_v2)` | custom |
| Wulfgar | +100% damage vs armored; breaking a chunk buffs all damage for 10 s | `increase_armored_damage`, `add_monster_hit_effects` on `break_armor` | generic key |

So the **"how many chunks per hit" side is mostly generic keys** (`increase_damage_against_monster_armor(_and_hits)`, `increase_damage_on_armor_and_hits`, `unique_hit_multiplier`, `increase_num_unique_hits`), but almost every use is **gated by a champion-specific condition** (debuff present, stacks, position, feat), which the per-champion adapters of ADR 0001 must model anyway. Gazrick, Lucius and Hew Maan need custom code.

Also note the multiplier stacking is presumably additive in extra segments (Grimm "stacking additively") but multiplicative with `unique_hit_multiplier` (unverified).

## 4. How the community builds formations for them

- **Swap in breakers for the armored boss**: Ezmerelda, Hew Maan (positioned so Kleeb is on top), Grimm, plus knockback/stun (Diana) [S3, Steam snippet "When I needed extra armor breaking, I switched in Esmeralda, Hew Mann, Grimm, and Diana"]. Gazrick for threshold reduction [S3]. The wiki's Favored Foes page keeps a reference table of these champions [WK].
- **DoT or many-hit ultimates**: Donaar, Krull, Torogar, Jaheira; multi-projectile ults Catti-brie, Birdsong, Nova; Corazón's ult [S1, S2, S3].
- **Raise BUD** (bigger per-hit damage clears bigger thresholds): gear, Artemis/Alyndra spikes, overkill ults (Penelope, Blooshi) [S1, S3].
- **Clear trash first** so all random-target hits land on the boss [S3].
- **Variants**: Breaker Breaker forces non-Breakers to base damage 1, so only the Breaker list matters there [GD restriction text].
- No calculator exists; players find out by trying ("hard to tell whether an armored boss can be beaten without trying", S2). byteglow has no monster or armour code.

## 5. Options for a strength metric and a reach/verdict estimate

All options share the detection of §2.4 and keep the push-area-reach estimate for damage-based monsters. They differ in how much of §1.3 they model.

### A. Flag with detail (status quo, improved)

Detect per adventure (and per area where area defines are available) and show "Armored boss at areas 20, 70, … — estimate stops before it" or "contains armored/hits monsters". Show the breakers the player owns as a hint, from the `breaking` tag and the four upgrade-granted ones.

- **Accuracy**: no false numbers; but since ~1 in 5 adventures has such a boss, many Push verdicts become "estimate stops at area N".
- **Cost**: low (a few dozen lines over GD + tag list).

### B. Threshold-only check for armour; hits-based = "no damage wall"

Treat hits-based monsters as never walling on damage. For armored monsters, compute chunkHP(a) = HP_monster(a) / chunks (×section-health modifiers) on the push-area-reach curve, and compare with the Main DPS's per-hit damage (BUD, or modelled per-hit damage from push-area-reach option B/C). Armoured reach = last armored-boss area where BUD ≥ chunkHP; overall reach = min(damage reach, armour reach).

- **Accuracy**: the damage check is as good as push-area-reach (log-scale, about ±5–15 areas). It ignores hit rate, so it is optimistic when only one champion qualifies (a 250 s kill may still be fine for an idle Push, but not with enraging bosses). For hits-based, optimistic if hit rate is very low.
- **Cost**: low on top of push-area-reach option C: one division and the chunk lookup.

### C. Breaking rate: time-to-kill from generic attack data

Add a second formation metric, **breaking rate** = Σ over champions whose per-hit damage ≥ chunkHP (for hits-based: all champions) of `hits/s × chunks per hit`, where hits/s comes from `cooldown`, `num_targets`, `projectile_count`, `unique_hit_multiplier`, and chunks per hit from generic breaker keys (`increase_damage_against_monster_armor(_and_hits)`, `unique_hit_multiplier`, `increase_num_unique_hits`). Kill time = chunks / breaking rate; the area passes if kill time ≤ T_armor (a stated Scoring Assumption, e.g. 60–120 s). Per-hit damage of non-Main-DPS champions comes from the strength model's per-champion damage (or "assume only the Main DPS qualifies").

- **Accuracy**: captures the main levers the community uses (breakers, multi-hit, attack speed). Ultimates and custom breakers (Gazrick, Lucius, Hew Maan) are approximate or Scoring Assumptions. Hits-based estimates become fairly good (no damage term). Probably ±1 boss area band past where B says "qualifies".
- **Cost**: medium. Needs the generic attack fields (already in GD), condition handling in the champion adapters for the 13 breakers, a T_armor default, and per-area spawn data (area defines) to know which areas to test.

### D. Breaking as a search objective

Use C's breaking rate as a second score in the formation search for adventures whose objective lies beyond an armored/hits boss: either a constraint ("must clear the armored boss at 70 within T") or a lexicographic tiebreak. This lets the search propose the community's swap (add Ezmerelda/Hew Maan/Grimm) on evidence rather than rules, in line with ADR 0001.

- **Accuracy**: as C; risk that small breaking gains displace large damage gains unless the objective is a constraint.
- **Cost**: C plus search integration (a second objective) and UI to explain why a weaker-damage formation was chosen.

### E. Calibrate from snapshots

Record armored-boss areas where runs stall (needs the open "Snapshot history" item) to fit T_armor and correct the chunk/threshold assumption (e.g. whether chunks really equal `health_modifier`, whether threshold = HP/chunks).

- **Accuracy**: improves with use; the only way to verify the inferred threshold formula without client code.
- **Cost**: medium, depends on snapshot history; a follow-up, not v1.

### Cross-cutting notes

- B is nearly free once push-area-reach C exists; C/D are where the breaker champions start to matter.
- Per-area placement needs `adventure_area_defines`, which only arrive for active campaigns via `getuserdetails`. Without them, any option can only say "this adventure has one".
- Rules that exclude these monsters (`!hits_based^!armor_based`) must be honoured by the adventure-modifier handlers of push-area-reach.

## Open uncertainties

- The exact threshold formula (max HP ÷ chunks) and chunks = `health_modifier` are inferred from data shape, restriction texts and community posts, not from client code.
- Whether one hit can ever remove more than one chunk without breaker effects: community posts conflict (S1 vs S1/S2); data phrasing favours "one".
- Whether clicks and familiar clicks count as hits, and how ult DoT ticks are counted for custom ult types.
- Whether random-target attacks prefer or avoid armored/hits targets (Windfall explicitly "prefers"; others unknown).
- The 9 monsters whose `armor_based` tag and property disagree: which one the game reads.
