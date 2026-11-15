# Idle Champions Advisor

A personal, local advisor that reads one player's Idle Champions progress and recommends formations per adventure and the next steps to grow the account.

## Language

**Player Snapshot**:
The player's account state at a point in time: owned champions, gear, feats, specializations, saved formations, modron cores and currencies.
_Avoid_: save, profile, user data

**Game Definitions**:
The game's own catalogue of champions, adventures, areas, formation layouts and restrictions, as published by the game.
_Avoid_: static data, game data

**History Record**:
A slim, credential-free note of the account's progress at one Refresh (totals, each running game's current run, and the owned state when it changed), kept so that progress and rates can be compared over time.
_Avoid_: snapshot history, log, backup

**Refresh**:
Fetching a new Player Snapshot (and Game Definitions when they changed) from the game servers, read-only, on app start or on request.
_Avoid_: sync, update, reload

**Recommendation Key**:
A hash of the recommendation-relevant part of the Player Snapshot (owned champions and their levels, gear, feats, specializations, familiars, modron cores, completion, best areas; never currencies). Recommendations are kept per Recommendation Key and Game Definitions checksum.

**Recompute**:
Computing every adventure × Run Goal again in the background after a Refresh changed the Recommendation Key or the Game Definitions. Until it finishes, the earlier recommendations stay visible, marked out of date.
_Avoid_: rebuild, reindex

**Run Goal**:
What a player wants out of a single run of an adventure: Push, Gem Farm or Gold Farm.
_Avoid_: mode, objective (objective means something else in the game)

**Push**: Run Goal of reaching the highest area possible on an adventure under its restrictions (including variant and patron restrictions); completing an adventure is a Push that reaches its objective area.
_Avoid_: Complete (not a separate Run Goal)

**Gem Farm**: Run Goal of maximising gems per hour by looping one adventure with resets; run speed (areas skipped, bosses per hour) is how it is achieved, not a separate goal.
_Avoid_: Speed, speed run

**Gold Farm**: Run Goal of maximising Favor earned per hour.
_Avoid_: gold farming for chests, gold (in-run gold is only the means)

**Favor**:
The per-campaign currency earned when an adventure is reset, sized by the in-run gold collected; unlike in-run gold it carries over between runs.
_Avoid_: gold, divine favor (when the campaign is meant generically)

**Running Event**:
The event currently live in the game, whose adventures can be started now. Every other event's adventures are still shown but marked Not running, and their completion is unknown.
_Avoid_: active event, current event, live event

**Main DPS**:
The one champion a formation is built around, whose damage the rest of the formation exists to buff; the game calls it the "BUD-setting DPS". Not necessarily a champion tagged dps, since restrictions can forbid those.
_Avoid_: carry, damage dealer, "the DPS" (when several dps-tagged champions are seated)

**Formation Recommendation**:
The complete setup the advisor proposes for one adventure and one Run Goal (champions and their seats, specializations, feats, familiar placement and modron core setup), built only from the Player Snapshot.
_Avoid_: best team, lineup

**Armoured Monster**:
A monster that loses one armour chunk per hit, and only from a hit whose damage meets that chunk's threshold; weaker hits do nothing and extra damage is lost.
_Avoid_: armoured boss (when non-bosses are meant too), tank

**Hits-based Monster**:
A monster that loses one segment per hit regardless of the hit's damage, so it is beaten by the number of hits, not their size.
_Avoid_: hit-count boss

**Breaker**:
A champion whose effects make hits against Armoured or Hits-based Monsters count for more (extra chunks per hit, extra hits, or bonus damage against armour); the game's own "breaking" role.
_Avoid_: armour breaker, debuffer

**Area Set**:
The game's fixed layout of which monsters appear in each area, shared by every adventure that uses it (a variant shares its base adventure's); the game only reveals it for adventures currently loaded in the player's game, so the advisor learns Area Sets one by one and keeps them.
_Avoid_: area data, monster placement (as a noun for the thing kept)

**Anchor Run**:
The live game instance whose measured biggest hit (BUD) a Push estimate is scaled from: preferably one with the same Main DPS as the recommendation, otherwise the one with the highest BUD.
_Avoid_: reference run, baseline

**Push Verdict**:
What the Push tab says about a Formation Recommendation reaching its adventure's objective area: Reached before, Likely, Borderline, Unlikely, or Estimate not available (with the reason).
_Avoid_: prediction, chance, completion estimate

**Scoring Assumption**:
A stated condition a Formation Recommendation's score rests on because the game decides it at play time (stack counts reached, expected value of random effects, how actively the player clicks or collects daily caps); always shown with the recommendation.
_Avoid_: guess, estimate

**Model Status**:
The advisor's confidence in one champion's reconstructed effects: Not Modelled, Verified (checked against the game or an outside reference), Check Failed, or Stale (the champion changed since it was verified).
_Avoid_: "modelled" as a bare adjective, supported, done

**Next Step**:
A single action that grows the account (a gear upgrade, feat, champion to get, modron core level, or adventure to run), measured on its own against the current Player Snapshot, ranked within one Run Goal by its gain across that goal's Focus Set, and always paired with the reason it is ranked where it is.
_Avoid_: tip, todo

**Focus Set**:
The adventures a Run Goal's Next Steps are measured against: the ones the player actually runs or is working towards for that goal, pinned by the player.
_Avoid_: favourites, main adventures
