# How can v1 search all formation placements fast enough?

Research for issue #11. The question: how big is the formation search space for a realistic account, and which search strategies find the optimum, or a provably near-optimal answer, fast enough for a local app? This document lists options with runtime estimates and optimality guarantees. It does not pick one.

Date: 2026-11-15. Vocabulary follows `GLOSSARY.md`. It builds on the damage and buff model in `research/formation-strength-model.md` (branch `research/formation-strength-model`, issue #4) and the data shape in `research/game-api-and-data-shape.md` (branch `research/game-api-and-data-shape`, issue #3).

## Sources

| ID | Source | Trust |
|----|--------|-------|
| SNAP | The `getuserdetails` response in the game's local `webRequestLog.txt` (one Player Snapshot). Only aggregate counts were extracted, using `python3 -I`. No credential values were read into any output. | Primary |
| GD | `downloaded_files/cached_definitions.json`, the client's cached full `getDefinitions` payload (179 `hero_defines`, 1,993 `adventure_defines`, 28 `campaign_defines`). | Primary |
| BENCH | A throwaway benchmark written for this ticket and not committed. It uses a real 10-slot layout geometry from GD (campaign 2's default layout), the real owned-per-seat counts from SNAP, and **random** buff values. It ran on OR-Tools 9.15 and Python 3.14 on this Mac, with 8 CP-SAT workers. | Primary for runtimes, synthetic for values |
| ORT-MCF | Google OR-Tools, ["Assignment as a Minimum Cost Flow Problem"](https://developers.google.com/optimization/flow/assignment_min_cost_flow) | Primary (solver vendor) |
| ORT-TEAMS | Google OR-Tools, ["Assignment with Teams of Workers"](https://developers.google.com/optimization/assignment/assignment_teams) | Primary (solver vendor) |
| ORT-CP | Google OR-Tools, ["CP-SAT Solver"](https://developers.google.com/optimization/cp/cp_solver) | Primary (solver vendor) |
| GO | [frzyc/genshin-optimizer](https://github.com/frzyc/genshin-optimizer), files `libs/game-opt/solver/src/split.ts` and `libs/pando/engine/src/optimization/prune.ts` (pushed 2026-11-14) | Primary for how that optimiser works |
| DFS | [DimaKudosh/pydfs-lineup-optimizer](https://github.com/DimaKudosh/pydfs-lineup-optimizer), file `pydfs_lineup_optimizer/solvers/pulp_solver.py` | Primary for how that optimiser works |
| FSM | `research/formation-strength-model.md` §2: byteglow, Emmotes and Script Hub findings | Prior research |

## 1. Search space for this account

### 1.1 Inputs from the Player Snapshot and Game Definitions

**Owned champions per seat** [SNAP, GD]: 32 owned out of 168 available, 179 defined.

| Seat | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Owned | 4 | 2 | 3 | 2 | 2 | 3 | 2 | 4 | 3 | 3 | 2 | 2 |
| Available in game | 15 | 14 | 15 | 14 | 14 | 14 | 14 | 14 | 14 | 14 | 13 | 13 |

Every seat has at least two owned candidates, so every seat is a real choice.

**Formation layout sizes** across 1,993 adventures [GD]. The layout is the adventure's own `formation` game change if it has one (881 adventures). Otherwise it is the campaign default (1,104). 8 adventures have neither.

| Slots | 9 | 10 | 11 | 13 |
|---|---|---|---|---|
| Adventures | 235 | 1,641 | 108 | 1 |

- There are **163 distinct layout geometries**, by slot coordinates.
- Campaign defaults are 10 slots (25 campaigns), 9 (2) and 11 (1).
- Layouts have 4 to 6 columns. A slot has 1 to 6 adjacent slots.
- **Blocked slots** (`slot_escort`): 333 adventures block 1 to 7 slots. 1,660 block none.

**Restrictions that filter candidates first** [GD], counted as adventures carrying the game change:

| Game change | Adventures |
|---|---|
| `only_allow_crusaders` | 553 |
| `force_use_heroes` | 343 |
| `slot_escort` | 325 |
| `slot_effects` | 113 |
| `random_crusader_damage` | 43 |
| `slot_escort_by_area` | 39 |
| `perma_death` | 24 |
| `disallow_crusaders` | 20 |

**Per-champion sub-choices** [SNAP, GD]:

- **Specializations.** 229 specialization groups exist across 168 champions, with at most 6 groups per champion and mostly 2 or 3 options per group. Specialization combinations per owned champion: median 3, maximum 12.
- **Feats.** Owned champions have 104 unlocked feats in total (median 3, maximum 6 per champion) and 0 to 3 feat slots.
- **Familiars and modron.** The account owns 3 familiars and 1 modron core save.

### 1.2 Raw size

A formation is a choice of at most one champion per seat, each placed in a distinct usable slot. For *k* usable slots and *n_s* owned champions in seat *s*:

`|space| = Σ_j e_j(n_1..n_12) · k!/(k−j)!`, where `e_j` is the elementary symmetric polynomial (choose *j* seats and one champion in each) and the last factor places them.

| | 9 slots | 10 slots | 11 slots | 12 slots |
|---|---|---|---|---|
| **This account**, all slots filled | 4.3e11 | **3.2e12** | 1.6e13 | 4.0e13 |
| This account, partial formations allowed | 9.4e11 | 9.8e12 | 8.3e13 | 5.6e14 |
| This account, distinct champion sets (no placement) | 1.2e6 | 8.8e5 | 4.0e5 | 8.3e4 |
| **Full roster** (every available champion owned), all slots filled | 1.6e18 | 6.9e19 | 1.9e21 | 2.7e22 |

- Adding a specialization choice per fielded champion (median 3) multiplies the 10-slot figure by about 3^10 ≈ 6e4, giving **~1.9e17**. Feats and familiars multiply it further.
- Restrictions shrink the space a lot where they apply. For example, `only_allow_crusaders` by tag removes whole seats.

**Brute force is out.** The full model scored at 4.5 µs per formation in Python [BENCH], so 3.2e12 formations would take about 170 CPU-days. Even compiled code at about 50 ns per formation needs about 2 CPU-days per adventure, before specializations.

### 1.3 Structure that makes it tractable

The strength model from issue #4 gives:

`score = DPS_base(d) × Π buffs that reach the main DPS d at slot p`

Taking logs turns this into a **sum**. Once the main DPS *d* and its slot *p* are fixed, each term falls into one of four classes:

1. **Selection-only terms.** Global multipliers (`global_dps_multiplier_mult` with target `all`, gear, feats, legendaries) depend only on *who* is fielded, not *where*. They become a per-champion weight.
2. **Unary positional terms.** A support *c* at slot *s* buffs *d* if *p* is in `targets(c, s)` (`adj`, `behind`, `col`, `next_col`, `distance`, `by_tags` and so on). The layout's `col` and `adj` data decide this, so it is a weight `w(c, s)`.
3. **Pairwise terms.** One support changes another support's contribution. Examples: "+X% to adjacent champions' abilities", or a buff that needs the target to be adjacent to a third champion. These are weights on pairs `(c1, s1, c2, s2)`.
4. **Count, set and stack terms.** These include `amount_expr` or `per_hero_expr` counting tagged champions in the formation, stack mechanics, and champion-unique keys. They need champion-specific handlers, and most are monotone in a count.

For the 32 owned champions, 183 formation abilities are reachable through their upgrades [GD]. Classified roughly by `effect_keys` fields (heuristic counts, not a parser):

- **Explicit positional targets**: 15 of the 183 abilities. The target values seen are `adj` 5, `behind` 4, `distance` 3, `col`, `next_col`, `prev_and_next_col` and `bud_setter` 2 each, plus single uses of `front_column`, `non_adj`, `self_and_ahead`, `next_two_col` and others.
- **Target `all`**: 25 effect keys.
- **No explicit target**: most effect keys. These are key-defined, usually self or global.
- **`amount_expr` / `per_hero_expr`** (class 4): 38 abilities.
- **Stack fields**: many abilities.

**Takeaway.** With *d* and *p* fixed, classes 1 and 2 dominate the placement problem, and they are *separable*: a sum of per-champion, per-slot weights. Exact polynomial algorithms exist for that (§3, option B). Classes 3 and 4 decide whether an exact method stays cheap.

**Outer loop.** The fixed *(d, p)* pairs number at most (DPS candidates) × (slots). With 6 DPS candidates and 10 slots that is 60. The `dps` tag covers 49 of 179 champions in GD, but per account and goal the shortlist is usually a handful.

Per-champion sub-choices fold into the weights when they are separable:

- **Specializations and feats.** Each (champion, spec combo, feat set) becomes a "variant" of that champion. The search keeps at most one variant per champion. A specialization that only changes the champion's own buff is resolved as `max` over its variants at each slot.
- **Modron core.** The core grid is fixed per save, so it is a constant multiplier per champion and seat, not a search variable.
- **Familiars.** With 3 familiars and a handful of placement targets, the choices can be enumerated outside the placement search.

## 2. What existing optimisers do

- **Idle Champions tools: none search.**
  - byteglow's formation page is a buff *calculator*: the user places champions, and it evaluates the buff product toward a chosen DPS.
  - byteglow's speed page simulates a given formation.
  - Emmotes routes are curated tables.
  - Script Hub and BrivMaster run formations the player built by hand [FSM].
  - A GitHub search for "idle champions formation" or "optimizer" found one inactive Next.js "formation builder helper" (Shooshte/idle-champions, last pushed 2023). Its README is the create-next-app template, so it is not evidence of a solver.
- **Genshin Optimizer** (gear-set optimiser with a multiplicative damage formula and one item per slot) uses **branch-and-bound over candidate sets** [GO]:
  - `splitSubwork` calls `prune(nodes, candidates, 'q', minimum, topN)`, then splits the remaining candidates into smaller groups. It splits by an "incomparable" key first, then into about 4 chunks of the largest slot ordered by a monotone weight.
  - `prune.ts` implements `pruneRange`: drop candidates that cannot meet minimum constraints in any build.
  - `pruneBottom`: drop candidates that are never in the top N, using monotonicity (inc/dec) per stat.
  - `reaffine`: algebraic simplification of the formula.
  - The guarantee is exact top-N: pruning only removes candidates that provably cannot make the top N.
- **pydfs-lineup-optimizer** (daily-fantasy lineups with **position slots**, one player per slot, team constraints) formulates the lineup as a **0/1 integer program**: binary variables, linear objective and constraints, solved with PuLP's CBC (`PuLPSolver`, `LpBinary`, `PULP_CBC_CMD`) [DFS]. This is the "seat"-style constraint shape, solved exactly by MIP.
- **OR-Tools' own guidance** for assignment-with-groups problems, "six workers are divided into two teams, and each team can perform at most two tasks", is CP-SAT or MIP [ORT-TEAMS]. Min-cost flow "can often return a solution faster than either the MIP or CP-SAT solver", but "MIP and CP-SAT can solve a larger class of problems" [ORT-MCF].
- **Pattern.** Optimisers in comparable games pair a domain decomposition with either exact pruning (B&B with monotone bounds) or an exact MIP or CP formulation. Nobody brute-forces. In Idle Champions specifically, nobody searches at all today.

## 3. Options

Runtimes are for one adventure and one Run Goal with this account (32 owned, 10 slots, 6 DPS candidates). Numbers marked *measured* come from [BENCH] with synthetic buff values. Its pairwise model is deliberately harsh: 15% of champions are "amplifiers" giving +50% to adjacent supports' buffs.

### A. Exhaustive enumeration with dominance pruning

- **Method.** Enumerate formations.
  - Fixing *(d, p)* and dropping champions dominated within their seat helps little at 2 to 4 per seat.
  - Treating symmetric slots as equivalent also helps little.
- **Runtime.** About 3e12 formations per adventure is infeasible: ~170 CPU-days in Python, ~2 CPU-days compiled. It is feasible only for heavily restricted adventures, roughly ≤ 1e8 formations.
- **Guarantee.** Exact under any scoring model, including opaque champion code.
- **Role.** A test oracle on tiny or restricted instances.

### B. Fix main DPS, then optimal placement by min-cost flow (separable model)

- **Method.** For each *(d, p)*, build the flow network source → seat (capacity 1) → champion or variant → slot (cost −log weight) → sink. The seat node enforces "one champion per seat number" exactly, which a plain assignment cannot. The best of the 60 solves is the answer.
- **Runtime.** *Measured*: 60 solves in **17 ms** total.
- **Guarantee.** **Exact for classes 1 and 2**, through min-cost-flow integrality. Classes 3 and 4 are ignored.
  - On the synthetic instance, the flow answer scored 32.6 log-units against a true optimum of 36.2, because it ignored the amplifier pairs.
  - With amplifier terms set to their *maximum possible* value, the same flow gives a valid **upper bound** per *(d, p)*.
- **Cost.** It needs the min-cost-flow solver from OR-Tools (Apache-2.0) or a ~100-line successive-shortest-path implementation.

### C. B + local search repair (anytime heuristic)

- **Method.** Start from B's answer per *(d, p)*. Hill-climb on the *full* model with slot swaps and same- or free-seat champion substitutions. Variants include beam search (keep the top-W partial formations seat by seat) and simulated annealing.
- **Runtime.** *Measured*: **~100 ms** for all 60 *(d, p)* in Python.
- **Guarantee.** None in general. On seed 1, the only seed where D finished, it reached the proven optimum. B's upper bounds give an **a-posteriori gap certificate**: `max UB − found` bounds how far from optimal it can be. The bound is loose when pairwise terms are large.

### D. Exact: per-(DPS, slot) CP-SAT/ILP with B's bounds for pruning (branch-and-bound outer loop)

- **Method.**
  - **Model.** For each *(d, p)*, binary `x[c,s]` with at-most-one per champion, slot and seat. Unary weights are linear. Pairwise terms use AND-variables `z ≤ x[c1,s1]`, `z ≤ x[c2,s2]`. Count terms use integer counts with piecewise-linear log tables.
  - **Integer scaling.** CP-SAT needs integers ("you must define your optimization problem using integers only" [ORT-CP]). Log weights are scaled, for example ×10⁴, which bounds the rounding error at about 0.01% per term.
  - **Ordering and pruning.** *(d, p)* pairs are processed in descending order of B's upper bound, and a pair is skipped once its bound ≤ the incumbent from C. This is the Genshin Optimizer pattern [GO], with a flow relaxation as the bound.
- **Runtime.** *Measured*:
  - About **0.5 to 1 s per (d, p) subproblem** with amplifier pair terms.
  - On one seed, **47 s per adventure** to prove optimality: 54 of 60 subproblems solved, 6 pruned by bound.
  - On seed 2, the same exact search with upper-bound pruning **ran over 20 minutes without finishing**, and the run was stopped. Runtime is therefore heavy-tailed: interaction-dense instances can blow past any interactive budget. A per-subproblem time limit then turns D into "best found plus a proven gap", not a proven optimum.
  - A single monolithic CP-SAT model that also chooses *d* and *p* did **not** prove optimality in 120 s (bound 65.5 vs 36.2). The decomposition matters.
  - Without pairwise terms, each subproblem is a pure flow structure and solves in milliseconds.
- **Guarantee.** **Provably optimal** under the modelled score: the solver reports `OPTIMAL`, or returns a bound and gap when time-limited. Opaque champion-unique mechanics (FSM §1.3) remain unmodelled whatever the solver.

### E. Precompute per adventure

- **Method.** Run B+C, or D, for every adventure and Run Goal when a new Player Snapshot is loaded, and cache the result keyed by (snapshot checksum, definitions checksum, adventure, goal).
  - Adventures sharing a layout geometry (163 distinct) and restriction set share all slot relations, so `targets(c, s)` tables are computed once per geometry.
  - Restrictions only filter candidates.
- **Runtime.**
  - B+C over all 1,993 adventures: about **3 to 4 min**. Over only unlocked or relevant ones: seconds.
  - D over all adventures: at least **1 day of CPU** at ~45 s each, and unbounded on hard instances (seed 2 exceeded 20 min). Realistic only on demand, or in the background for the handful of adventures the player runs, each with a time limit.
- **Guarantee.** Inherits B, C or D's guarantee. Results become stale when the snapshot changes.

### F. Goal-specific narrowing (Gem Farm, Speed)

FSM §3 finds that Gem Farm and Speed are template-like: Briv, speed champions and familiar placement, plus a "good-enough DPS" threshold. Formulated as constraints, these become a satisfiability or small-enumeration problem:

- the required champions are fixed via `force`-like constraints;
- the DPS score must be ≥ the threshold, a single linear constraint in D;
- the remaining slots maximise a speed objective.

- **Runtime.** Milliseconds.
- **Guarantee.** Exact within the template. The template itself is curated, so it carries no guarantee.

### Summary

| Option | Runtime per adventure (this account) | Guarantee | Main cost or risk |
|---|---|---|---|
| A Exhaustive | days (infeasible) except tiny restricted cases | exact, any model | only usable as a test oracle |
| B Fix DPS + min-cost flow | ~20 ms | exact for selection and positional terms; upper bound otherwise | ignores pairwise and count effects |
| C B + local search / beam | ~100 ms | none; a-posteriori gap via B's bound | may miss the optimum on interaction-heavy formations |
| D Per-(DPS, slot) CP-SAT/ILP + bound pruning | ~0.5–1 s per subproblem; 47 s per adventure on seed 1, but over 20 min without finishing on seed 2 (measured) | provably optimal under the modelled score when it finishes; otherwise best found plus gap | OR-Tools dependency; integer scaling; model-building effort for pairwise and count terms |
| E Precompute | B+C: minutes for all adventures; D: background or on demand, time-limited | same as the method used | staleness; cache keying |
| F Goal templates | ms | exact within the template | template curation |

**Scaling note.** For a future full roster (~14 champions per seat), B grows only polynomially, to about 170 champion nodes. C grows linearly per move. D's subproblems grow to about 1,700 `x` variables and need per-seat dominance pruning to stay at seconds. A is hopeless.

## Open uncertainties

- The benchmark uses **random buff values** on a real geometry. Real champions' pairwise and count effects may be sparser, which makes D faster and C more often optimal, or denser. A rerun on real effect data is needed once an effect interpreter exists.
- The share of score that is truly separable comes from heuristic classification of `effect_keys` fields, not a parser.
- Whether filling every slot is always optimal is assumed: log weights ≥ 0. Effects with negative contributions, such as `random_crusader_damage` and some patron or slot effects, would break that, and B would then need optional slots (supply < slots).
- Runtimes were measured on one Mac with 8 CP-SAT workers. A slower machine scales roughly linearly. Only seed 1 finished the exact search; seeds 3–5 were never run, so D's typical-case runtime is unknown beyond "47 s once, more than 20 min once".
- No Idle Champions formation *search* tool was found to compare against.
