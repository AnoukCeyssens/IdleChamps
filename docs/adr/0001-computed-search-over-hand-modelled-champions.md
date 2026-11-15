# Formation Recommendations come from a computed search over every placement, with every champion modelled by hand

The advisor compares every legal placement of the player's owned champions (with specializations, feats, familiars and modron cores) for each Run Goal, scoring each with a model in which every champion's effects are reconstructed by hand from Game Definitions, effect descriptions and community math, then validated against in-game numbers. We chose this over curated rules or a tiered "model only the meta" approach because the player wants the best outcome to win on evidence, not on community convention, and because the same model then yields measurable "what would make this better" hints for Next Steps. Curated formations (Emmote's repos, our rules file) only seed the search and serve as a check: a computed result that loses to one flags a model bug.

## Considered Options

- **Curated rules only**: accurate for Gem Farm, but can't rank unusual rosters or quantify hints.
- **Tiered model** (meta and owned champions only, the rest marked unmodelled): far less upkeep, but doesn't meet "every champion".
- **Porting the game client's own handler code**: the most exact, but rejected over terms-of-service and legal risk, and because ported code couldn't live in this public repo.
- **Defer to byteglow**: lowest upkeep, but gives Next Steps nothing to build on.

## Consequences

- Roughly 400–700 champion adapters to write and about 2–3 champions a month to keep current; adapters are built owned-first, then meta, then the rest, and an unfinished champion is flagged "not yet modelled".
- About 150 champions can only be exact under a stated Scoring Assumption (stack counts, expected value of random effects, player activity); these assumptions are always shown to the player.
- Brute force is infeasible (~3e12 placements per adventure), so the default search is approximate with a shown gap; a provably-best exact solve is on demand and time-limited.
