# How can formation strength be evaluated?

Research for ticket 03 (formation-advisor). Question: how does Idle Champions damage/buff math work, how much of it is in **Game Definitions** versus only in effect text, how do existing tools approach it, and what do **Gem Farm** / **Speed** depend on compared to **Push** / **Complete**. The deliverable is a set of feasible options with accuracy and maintenance cost. It does not pick one.

Date: 2026-11-15. Vocabulary follows `GLOSSARY.md`.

## Sources

| ID | Source | Trust |
|----|--------|-------|
| GD | The game's own `getDefinitions` payload, cached locally as `downloaded_files/cached_definitions.json` (21 MB, 200 tables). Only its structure and effect definitions were inspected. The game's `webRequestLog.txt` holds a delta copy plus the `getuserdetails` response; only key names were read from it, never credential values. | Primary |
| BG | ic.byteglow.com production JS bundle (`assets/shared-*.js`, `speed-*.js`, `formation-*.js`), read as minified source. | Primary for how that tool works |
| EM | [Emmotes/ic_scripting_routes](https://github.com/Emmotes/ic_scripting_routes) `docs/scripts/main.js` (pushed 2026-11-08) | Primary for its own math |
| MB | [mikebaldi/Idle-Champions](https://github.com/mikebaldi/Idle-Champions) `AddOns/IC_BrivGemFarm_Performance/README.md` (Script Hub, the standard gem-farm script) | Primary for script behaviour |
| RL | [RLee-EN/BrivMaster](https://github.com/RLee-EN/BrivMaster) (newer gem-farm script, pushed 2026-11-12). File list only. | Existence and recency |
| WK | Wiki: `idlechampions.fandom.com/wiki/Formation_strategy`. **Not reachable**: HTTP 402 to automated fetches. Nothing here is cited from it. | n/a |

Counts like "3089 effects" were computed by a throwaway Python script (`python -I`) over GD on 2026-11-15. They will drift with game updates.

## 1. How the damage and buff model is encoded in Game Definitions

### 1.1 The building blocks

- **Champion base**: `hero_defines` has `base_damage`, `base_cost`, `cost_curves`, `base_health`, `base_attack_id`, `ultimate_attack_id`, `seat_id`, and `tags` [GD]. The tags include the role tags `dps` (49 champions), `support` (148), `speed` (23), `gold` (36), `positional` (88), `tanking`, `healing` and `control`. The role vocabulary is itself defined in `game_rule_defines.role_tags_v2` [GD].
- **Attacks**: `attack_defines` has `damage_modifier`, `cooldown`, `target`, `num_targets`, `aoe_radius` and `damage_types` (melee, ranged or magic) [GD].
- **Level-gated upgrades**: `upgrade_defines_*` has 14,669 rows: `hero_id`, `required_level`, `upgrade_type`, `effect` (a compact effect string), `static_dps_mult`, and `specialization_name`/`specialization_description` (647 specialization choices) [GD]. Example, Briv:
  - `hero_dps_multiplier_mult,400`: self DPS ×5 at levels 30, 60 and so on.
  - `global_dps_multiplier_mult,100`: all champions ×2 at levels 40, 130 and so on.
  - `buff_upgrade,100,3451`: raises another upgrade's effect by 100%.
  - `effect_def,568`: unlocks a formation ability defined in `effect_defines`.
- **Abilities**: `effect_defines_*` has 3,089 rows. Each has `effect_keys`: a list of objects with an `effect_string` (`key,params…`) and optional `targets`, `filter_targets`, `amount_expr`, `per_hero_expr`, `stack_func`, `amount_func`, `max_stacks`, `off_when_benched`, triggers, and more [GD]. Each also has `description` text with `$amount` placeholders and `properties` such as `is_formation_ability` or `is_positional_ability`.
- **Effect-key catalogue**: `effect_key_defines` has 1,023 keys, each with an `owner` (`hero`, `global`, `area`, `upgrade`, `monster` and so on) and a description template. Example: `global_dps_multiplier_mult` → "Increases the damage of all Champions by $amount%" [GD].
- **Gear**: `loot_defines_*` has 4,392 items, each with `hero_id`, `slot_id`, `rarity` and `effects`. The effects are the same strings as above: `global_dps_multiplier_mult`, `buff_upgrade,<pct>,<upgrade_id>`, `reduce_ultimate_cooldown`, `health_mult` and so on. The base value per rarity is common 10, uncommon 30, rare 50, epic 100 (Briv slot 4: `buff_upgrade,10|30|50|100,3452`) [GD]. Item level and gilding scale this. EM implements `effect = base × rarityFactor × gild × (1 + (ilvl-1)/250)`, with gild shiny ×1.5 and golden ×2 (sic, from `determineJumps`) [EM]. The Player Snapshot carries each item's `rarity`, `enchant`, `gild` and `pigment`.
- **Feats**: `hero_feat_defines` has 2,675 rows with the same effect strings: mostly `buff_upgrade` and `global_dps_multiplier_mult`, plus `increase_ability_score`, `add_hero_tags`, and `effect_def` for unique feats [GD].
- **Legendary effects**: `legendary_effect_defines` has 110 rows, mostly `global_dps_multiplier_mult,100` with `targets:["active_campaign"]` [GD].
- **Formation layout**: an adventure's or campaign's `game_changes` entry of `type:"formation"` lists the slots, each with `x,y,col,row,adj:[…]` [GD]. So adjacency, column and row are explicit data, not something to infer.
- **Restrictions**: an adventure's `game_changes` covers variants and patrons. Its types include `only_allow_crusaders`, `force_use_heroes`, `slot_effects`, `global_effects`, `random_crusader_damage`, `perma_death` and `slot_escort`, alongside the human-readable `restrictions_text` [GD].
- **Familiars and modron**: `familiar_defines` has 357 rows, but they are cosmetic and cost data with no effect strings. Familiar behaviour (clicking, levelling, field slots) is hard-coded game logic plus `game_rule_defines` entries such as `bg_familiar_rate_multipliers` and `auto_progress_familiar_slot`. `modron_core_defines` and `modron_tile_defines` describe the core grid and node multipliers, and `max_modron_auto_reset_area` is 2500 [GD].

### 1.2 The damage formula

The game's combat code is not published. The structure of the data and of community tools implies the following model:

```
champDPS ≈ base_damage(level, cost curve) × attack.damage_modifier / attack.cooldown
         × Π (hero_dps_multiplier_mult from own upgrades/feats/gear)
         × Π (global_dps_multiplier_mult from every champion's upgrades/gear/feats/legendaries on the field)
         × Π (positional effects from other champions whose `targets` include this seat, after buff_upgrade chains)
         × (stack-based effects: per-kill, per-hit, per-area, Steelbones-style)
         × (enemy/area modifiers: favored foe, debuffs, game_changes)
```

- Buffs from different sources multiply. Each `*_mult,N` means ×(1+N/100).
- `buff_upgrade,P,U` raises upgrade U's amount by P%. Gear, feats and level upgrades mostly reach a champion's power this way, so a support's buff value is a chain of `buff_upgrade` effects applied to one base ability.
- Because effects compound multiplicatively, formation strength is in practice "main DPS damage × product of buffs that reach the DPS seat". This is the model byteglow's formation tool uses (see §2).
- Monster HP/armor per area is in `game_rule_defines.monster_base_stats` and `monster_defines`. That gives a "can it clear area N" check for Push and Complete, but only as an approximation, because healing, tanking and control decide survival rather than DPS alone.

### 1.3 How much is expressible from data versus effect text

| Aspect | Expressible from GD | Notes |
|---|---|---|
| Slot geometry (adjacency, column, row) | Fully | `formation[].adj/col/row` per adventure or campaign |
| Targeting of an effect | Mostly | `targets` vocabulary has ~40 forms: `all`, `adj`, `behind`, `ahead`, `col`, `next_col`, `prev_col`, `prev_two_col`, `front_column`, `edge`, `{type:distance}`, `{type:by_tags}`, `{type:attack_type}`, `{type:heroes}`, `{type:cascade…}`, plus champion-specific ones (`snowflake`, `bud_setter`, `adj_behind_propagate_species`) |
| Static multipliers (upgrades, gear, feats, legendaries) | Fully | Generic keys (`global_/hero_dps_multiplier_mult`, `buff_upgrade(s)`) |
| Specialization choices | Fully (as data) | Specialization upgrades carry `specialization_name` and an effect string |
| Conditional and filtered buffs | Mostly | `filter_targets` and `target_filters` (tag, stat score, attack type, affected-by-upgrade), and `amount_expr` such as `upgrade_amount(1656,0)+…` |
| Stack and trigger mechanics | Partly | `stack_func`, `amount_func`, `stacks_on_trigger` and `max_stacks` are declared, but how fast stacks build depends on runtime (kills, hits, time) |
| Champion-unique mechanics | Opaque | 492 of the 1,199 distinct effect keys are champion-prefixed (e.g. `briv_unnatural_haste`, `hero_dps_mult_per_briv_steelbones`). They cover 613 of 4,910 effect-key uses. Their semantics live in game code, and the only explanation is the `description` text |

Of the 2,276 effects flagged `is_formation_ability`, about **1,221 (54%)** use only generic, static keys with no expression, stack or trigger fields. The rest need per-key interpretation or per-champion code, and these are disproportionately the meta-defining champions (DPS stack engines, Briv, the big supports).

Description text is templated (`$amount`, `$(not_buffed amount)`, `^$other_description`). It can render tooltips, but it is not a reliable source of math.

## 2. How existing tools approach it

- **byteglow (ic.byteglow.com), the de facto community tool.** It pulls the player's account by user id and hash, and has pages for formation, speed, modron, legendaries and support [BG].
  - *Formation page*: this is a buff calculator, not a solver. The user places champions and picks a main DPS. The tool evaluates each effect toward that DPS from GD (function `Fu`): it handles `filter_targets` and `amount_func` with attack-type, stat-score and tag filters, and returns `-1` (unknown) where a feat or specialization may change eligibility. Modron damage nodes are evaluated the same way (`isDpsNode`, `applicable`).
  - *Speed page*: a Monte-Carlo simulation of Briv jumps and walks over an adventure's quest layout, with outputs `min/max/avg`, `perfectSkip` and `canSkipBosses`. It is combined with **hand-coded per-champion speed modules** (settings `speedBriv`, `speedHewMaan`, `speedMelf`, `speedNordom`, `speedTatyana`, `speedVirgil`, `speedDiana`, `speedDynaheir`, `speedEllywick`, `speedWiddle`, `speedCore`).
  - In short: generic effect evaluation, plus curated code for the champions that matter. It never searches over all formations.
- **Emmotes scripting routes.** Gem Farm route planner with closed-form Briv math [EM].
  - Skip chance: `effect = 0.25·(1 + rarity·gild·(1+ilvl/250))`; halve while above 1 to count skips. Item-level presets: `ilvl = 500·2^(j-1) − 374` for a "j-jump" Briv.
  - Stack need per jump: `stacks = ceil((stacks − 0.5)/0.968)` starting from 48. 0.968 corresponds to the Metalborn spec's 3.2% consumption, which matches `effect_def 573` in GD.
  - **Routes are curated**: named routes such as `pure4TT` or `feat94TT`, each a 50-zone jump bitmap per supported adventure (`advData/{3,605,695,1050,1137,1163}.json`). Formation advice is text and checkbox driven: Tatyana in M, Widdle adjacencies, familiars in Q/E/M, Thellora favour.
- **Gem-farm scripts** (Script Hub BrivGemFarm [MB], BrivMaster [RL]).
  - These do not evaluate formations at all. The player hand-builds formation saves: Q is speed with Briv and a familiar; W stacks with no familiars; E is Q without Briv.
  - The script automates swaps, offline Steelbones stacking (a ~9–15 s client close that triggers the offline catch-up) and the Modron reset.
  - Its tunables (stack zone = reset zone − 4/6/8/10 by jump count) are rules of thumb, and it points users to byteglow's speed page for stack targets [MB].
- **Wiki and community.** Formation advice is curated prose: champion pages, "Formation strategy", Discord and reddit. The wiki was not machine-readable from here (HTTP 402).

**Pattern across tools.** Nobody ships a general "best formation" optimiser. Practitioners rely on curated knowledge (which DPS, which supports, which speed champions, which routes). Tools then compute narrow, well-understood quantities: buff product to a chosen DPS, Briv skip odds and stacks, modron node values.

## 3. What each Run Goal depends on

| Run Goal | Dominant factors | Computable? |
|---|---|---|
| **Push** | Main DPS damage × buff product at the DPS seat; survivability (tank, healing, control) at high areas; favored-foe and area modifiers; stack-builders' ramp time | Buff product: mostly yes. Survival and ramp: weakly, heuristics only |
| **Complete** | Same as Push, but only up to the completion area, under restrictions (`only_allow_crusaders`, `force_use_heroes`, slot effects, patron rules). Feasibility first, strength second | Restrictions: yes (GD). Strength threshold vs monster HP: approximate |
| **Gem Farm** | Gems/hour = bosses/hour. Driven by Briv: skip count and chance (gear slot 4 ilvl, rarity, gild, Unnatural Haste, specs and feats), Haste/Sprint stacks vs route, stack zone, Modron reset area (≤2500), Thellora favour/Rush, offline stacking. Plus speed champions (Hew Maan, Shandie dash, Melf, Tatyana, Nordom, Dynaheir, Ellywick, Widdle and others) and familiars on field and in Q/E/M. DPS only needs to one-shot up to the reset area | Briv math: yes (closed form, EM). Speed champions: per-champion code (byteglow pattern). Route choice: curated table |
| **Speed** | Same engine as Gem Farm (Briv, speed champions, familiars). The target metric is clear time or bosses/hour rather than gems/hour | Same as Gem Farm. The map's rule "collapse if same formation" likely applies; the difference is mostly reset area and patron context |
| **Gold Farm** | Gold-tagged champions (`gold` tag, 36) and `gold_multiplier_mult` effects (43 ability uses, plus gear and feats) [GD]; chest drops per boss, so speed matters too | Gold multipliers: yes (generic key). Chest yield: approximate |

Gem Farm and Speed formations are mostly *speed-champion selection plus Briv configuration plus familiar placement plus Modron core setup*, with a "good-enough DPS" constraint. Push and Complete are *buff-product maximisation plus a survival check plus restriction feasibility*.

## 4. Feasible options

### A. Computed model: generic effect interpreter plus search

Implement the effect-string interpreter for generic keys (§1.3, about 54% of formation abilities), using the targets vocabulary and slot geometry. Score formation = DPS output of the best DPS candidate, then search over placements (beam search or ILP; seats limit one champion per seat, so the space is constrained).

- **Accuracy**: good on the static buff product for "plain" supports. Wrong or missing for stack, trigger and champion-unique keys, and those are disproportionately today's meta DPS and supports. It cannot judge survival or ramp time. Expect plausible but often non-meta answers for Push, and near-useless answers for Gem Farm without Briv-specific code.
- **Maintenance**: medium-high up front. Each new champion with a new key needs interpretation, about one champion every 2–4 weeks. New generic keys are rare and self-update from GD.
- **Risk**: false confidence, because the scores look precise.

### B. Curated rules

A hand-maintained knowledge file per role and goal:
- DPS tiers;
- support packages ("X adjacent to DPS", "Y in column behind");
- speed-team templates;
- Briv route table (EM-style);
- familiar and modron templates.

These are matched against owned champions and the adventure's formation layout and restrictions from GD.

- **Accuracy**: as good as the curator's sources and currency. High for Gem Farm and Speed, where community consensus is strong and templated. Medium for Push and Complete, because rules don't capture the player's gear levels or partial investment.
- **Maintenance**: continuous manual curation tracking balance patches, new champions and events. Source choice is ticket 01. For one player this is feasible only if scoped to the champions they own.

### C. Hybrid (the pattern byteglow uses)

Curated rules choose *roles and candidates* (main DPS shortlist, speed team, Briv route). A computed layer handles the parts the data does well:
- seat placement to maximise the buff product at the chosen DPS seat, using generic effects plus hand-coded handlers for the ~10–20 meta-relevant unique keys;
- restriction feasibility from `game_changes`;
- Briv skip and stack math (closed form);
- ranking by the player's real gear, feats and specializations from the Player Snapshot.

Unknown or opaque effects are reported as "not modelled" rather than scored as 0. This mirrors byteglow's `-1`.

- **Accuracy**: best available. It also yields "what would make this better" deltas for Next Steps: gear ilvl → buff, Briv ilvl → jump tier.
- **Maintenance**: medium. A small curated file, plus per-champion handlers only for champions that matter to this account. Generic parts update themselves from GD.

### D. Defer to existing tools

Recommend from curated rules only, and deep-link to byteglow pages for computation.

- **Accuracy**: depends on byteglow.
- **Maintenance**: lowest.
- **Trade-off**: weakest integration with Next Steps; depends on a third-party site's URLs and formats.

### Cross-cutting notes for the decision

- Generic geometry and multiplier interpretation (from GD) is a prerequisite for A and C. It is also cheap to validate against byteglow's formation page for the same champions.
- Briv math is small, closed-form and well sourced [EM]. Under any option it can be computed rather than curated.
- Survival and ramp for Push are not modelled by any tool found. Treat them as curated thresholds or ignore them in v1.
- Accuracy validation (the map's "Validating recommendations" item) can use the player's own `formation_saves_v2`, which include formation, familiars, feats and specializations, as a baseline: the Player Snapshot exposes them.

## Open uncertainties

- The exact in-game formulas (rounding, additive vs multiplicative for `_add` keys, the item-level scaling constant) are inferred from community code, not from the game source.
- The wiki could not be read (HTTP 402). The champion-specific guidance that lives there was not checked.
- byteglow's code was read minified. Its function names are obfuscated, so the description above is behavioural.
