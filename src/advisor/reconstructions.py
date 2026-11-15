"""Per-champion reconstructions (#40, ADR-0001): champion-unique effects, rebuilt by hand.

Each reconstruction reads its champion's ability keys (`champion_models.AbilityKey`: the effect key,
its parameters and fields, with `buff_upgrade` buffs from gear, feats and upgrades already applied)
and returns `Reconstructed` effects in the Scorer's vocabulary, never anything champion-specific
outside this module. Amounts come from the Game Definitions; the rules come from the champion's
description text and community math (research/champion-effect-modelling.md, armoured-hits-based-
monsters.md, formation-strength-model.md). A key a reconstruction doesn't find adds nothing.

Every reconstruction names its description tests (tests/test_reconstructions.py: expected values from
the description, at given gear and stacks) and has tooltip fixtures (tests/fixtures/tooltips.json).
Those plus a passing live damage check earn it Verified (`advisor.model_status`).

Batch 1 (meta first, owned first): Kas (the recording's Main DPS), Briv (Gem Farm area skips), and the
Breakers Gazrick, Lucius and Hew Maan. Next batch candidates (most-used owned meta champions in the
recording's running formations, then meta Gem Farm speed champions): Kalix, Hank, Spurt, Duke
Ravengard, Lae'zel, Havilar, Strongheart, Melf, Catti-brie; then Thellora, Widdle, Tatyana, Dynaheir,
Sentry, Nordom, Virgil, Diana and Ezmerelda (Breaker).

Effect key shapes: the recording has no `effect_defines`, so the keys below follow the parameter
names the Game Definitions publish in `effect_key_defines` where known (`briv_unnatural_haste`:
amount, required_stacks, stack_consume_percent, skip_chance, skip_amount) and research notes
otherwise; they are unverified until a live Refresh brings the real definitions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from advisor.champion_models import (
    AbilityKey,
    ExplanationLine,
    Reconstructed,
    Reconstruction,
    Skips,
    _BreakEffect,
    _Effect,
    skip_landings,
)

BOTH = frozenset({"armoured", "hits"})
ARMOURED = frozenset({"armoured"})


def _first(keys: tuple[AbilityKey, ...], name: str) -> AbilityKey | None:
    return next((k for k in keys if k.key == name), None)


# --- Kas ------------------------------------------------------------------------------------------

KAS = 153
KAS_BORN_INTO_EVIL = 2081  # effect_def of Kas's "Born Into Evil" (upgrade 15619 in the recording)


@dataclass(frozen=True)
class Kas(Reconstruction):
    """Born Into Evil: "Kas increases his damage for each Evil Champion in the formation, stacking
    multiplicatively." Its damage key's amount, per seated Evil champion (Kas included)."""

    def build(self, keys: tuple[AbilityKey, ...]) -> Reconstructed:
        key = next((k for k in keys if k.effect_id == KAS_BORN_INTO_EVIL and k.key == "hero_dps_multiplier_mult"), None)
        amount = key.amount(0) if key else None
        if amount is None:
            return Reconstructed(lines=(ExplanationLine("Born Into Evil per Evil champion", None, "Born Into Evil: no definition loaded."),))
        return Reconstructed(
            effects=(_Effect("damage", math.log1p(amount / 100), ("self",), per=("tag", "evil")),),
            lines=(ExplanationLine(
                "Born Into Evil per Evil champion", amount,
                f"+{amount:g}% damage per Evil champion in the formation, Kas included, stacking multiplicatively",
            ),),
        )


# --- Briv -----------------------------------------------------------------------------------------

BRIV = 58
HASTE_REQUIRED_STACKS = 48  # stacks Briv keeps to jump at all (community math, EM)


@dataclass(frozen=True)
class Briv(Reconstruction):
    """Unnatural Haste: after each area, a chance to skip areas. The skip value is the key's amount
    (%) raised by its buffs (slot-4 gear); while above 100% it is halved and one more area is skipped
    (EM's closed form). Each jump spends `stack_consume_percent` of his Haste stacks (3.2% with
    Metalborn's override). Steelbones (damage per stack behind him) is not modelled: during Gem Farm
    the stacks go into jumps."""

    def build(self, keys: tuple[AbilityKey, ...]) -> Reconstructed:
        haste = _first(keys, "briv_unnatural_haste")
        if haste is None or haste.amount(0) is None:
            return Reconstructed(lines=(ExplanationLine("Skip chance", None, "Unnatural Haste: no definition loaded."),))
        value = haste.amount(0) / 100
        areas = int(haste.raw(4) or 1)
        while value > 1:
            value /= 2
            areas += 1
        skips = Skips(areas, round(value, 6))
        override = _first(keys, "briv_unnatural_haste_stack_consume_percent_override")
        consume = (override.raw(0) if override else None) or haste.raw(2) or 4.0
        required = haste.raw(1) or HASTE_REQUIRED_STACKS
        stacks = required
        for _ in range(10):
            stacks = math.ceil((stacks - 0.5) / (1 - consume / 100))
        played = skip_landings(skips)[100]
        return Reconstructed(
            skips=skips,
            lines=(
                ExplanationLine("Skip chance", 100 * skips.chance, f"{100 * skips.chance:g}% chance to skip {areas} areas"),
                ExplanationLine("Areas skipped", areas, f"{areas} areas per jump"),
                ExplanationLine("Haste stacks used per jump", consume, f"{consume:g}% of his Haste stacks per jump"),
                ExplanationLine("Haste stacks for 10 jumps", stacks, f"{stacks} Haste stacks last 10 jumps"),
                ExplanationLine("Areas played to area 100", played,
                                f"about {played:.1f} areas played in a run to area 100 (Monte-Carlo)"),
            ),
            assumptions=(("Gem Farm", (
                f"Briv: {100 * skips.chance:g}% chance to skip {areas} areas after each area, simulated by "
                "Monte-Carlo (200 runs, fixed seed); a failed roll walks one area. Skipped areas count as completed, "
                "bosses included. His Haste stacks are rebuilt by one 15 s offline stack per run."
            )),),
        )


# --- Breakers -------------------------------------------------------------------------------------

GAZRICK = 98
LUCIUS = 72
HEW_MAAN = 75


@dataclass(frozen=True)
class Gazrick(Reconstruction):
    """Armor Ablation: "cold attacks lower the armour threshold by X%, stacking". Taken at max_stacks
    (stacking multiplicatively); lowers the chunk health a hit must reach in the Push Verdict's
    breaking check."""

    def build(self, keys: tuple[AbilityKey, ...]) -> Reconstructed:
        key = _first(keys, "gazrick_armor_ablation_v2") or _first(keys, "gazrick_armor_ablation")
        pct = key.amount(0) if key else None
        if pct is None:
            return Reconstructed(lines=(ExplanationLine("Armour threshold reduction", None, "Armor Ablation: no definition loaded."),))
        stacks = int(key.fields.get("max_stacks") or 1)
        ablation = 1 - (1 - min(pct, 99.0) / 100) ** stacks
        return Reconstructed(
            armour_ablation=ablation,
            lines=(ExplanationLine(
                "Armour threshold reduction", 100 * ablation,
                f"armour chunks need {100 * ablation:.4g}% less damage per hit ({pct:g}% × {stacks} stacks)",
            ),),
            assumptions=(("Push", f"Gazrick: Armor Ablation at its {stacks} stacks on every armoured boss."),),
        )


@dataclass(frozen=True)
class Lucius(Reconstruction):
    """Acid Splash removes extra segments per hit (his own hits); armour-eating acid multiplies the
    armour every hit removes (all champions' hits on Armoured bosses)."""

    def build(self, keys: tuple[AbilityKey, ...]) -> Reconstructed:
        acid = _first(keys, "lucius_armor_eating_acid")
        multiplier = _first(keys, "unique_hit_multiplier_from_attacks")
        extra = acid.amount(0) if acid else None
        mult = multiplier.amount(0) if multiplier else None
        effects, lines = [], []
        if extra:
            effects.append(_BreakEffect("extra", extra, BOTH, ("self",)))
            lines.append(ExplanationLine("Extra segments per hit", extra, f"his hits remove {extra:g} extra segments"))
        if mult:
            effects.append(_BreakEffect("mult", mult, ARMOURED, ("all",)))
            lines.append(ExplanationLine("Armour loss multiplier", 1 + mult / 100,
                                         f"armoured bosses lose ×{1 + mult / 100:g} chunks per hit"))
        return Reconstructed(
            break_effects=tuple(effects), lines=tuple(lines),
            assumptions=(("Push", "Lucius: his acid is on every armoured boss he attacks."),) if effects else (),
        )


@dataclass(frozen=True)
class HewMaan(Reconstruction):
    """Kleeb on top: from the middle columns, attacks on bosses destroy more segments (his
    `unique_hit_multiplier`, every champion's hits). The spawn-with-hits-missing passive and Yassa's
    attack speed are not modelled."""

    def build(self, keys: tuple[AbilityKey, ...]) -> Reconstructed:
        key = _first(keys, "unique_hit_multiplier")
        pct = key.amount(0) if key else None
        if not pct:
            return Reconstructed(lines=(ExplanationLine("Boss hit multiplier", None, "Kleeb: no definition loaded."),))
        return Reconstructed(
            break_effects=(_BreakEffect("mult", pct, BOTH, ("all",), where="middle_columns"),),
            lines=(ExplanationLine("Boss hit multiplier", 1 + pct / 100,
                                   f"×{1 + pct / 100:g} segments per hit on bosses while he sits in a middle column"),),
        )


_T = "tests/test_reconstructions.py::"

BATCH_1 = {
    KAS: Kas(
        description_tests=(_T + "test_kas_gains_damage_per_evil_champion_stacking_multiplicatively",),
        claims_effects=frozenset({KAS_BORN_INTO_EVIL}),
    ),
    BRIV: Briv(
        description_tests=(
            _T + "test_briv_skip_value_halves_above_100_percent_adding_an_area",
            _T + "test_briv_metalborn_spends_fewer_stacks_per_jump",
            _T + "test_briv_monte_carlo_matches_the_expected_walk",
            _T + "test_briv_skips_raise_gems_per_hour_and_state_the_monte_carlo",
        ),
        claims_keys=frozenset({"briv_unnatural_haste", "briv_unnatural_haste_stack_consume_percent_override"}),
        speed_keys=frozenset({"briv_unnatural_haste"}),
    ),
    GAZRICK: Gazrick(
        description_tests=(_T + "test_gazrick_ablation_lowers_the_armour_threshold",),
        claims_keys=frozenset({"gazrick_armor_ablation", "gazrick_armor_ablation_v2"}),
    ),
    LUCIUS: Lucius(
        description_tests=(_T + "test_lucius_acid_breaks_armoured_bosses_faster",),
        claims_keys=frozenset({"lucius_armor_eating_acid", "unique_hit_multiplier_from_attacks"}),
    ),
    HEW_MAAN: HewMaan(
        description_tests=(_T + "test_hew_maan_doubles_segments_only_from_a_middle_column",),
        claims_keys=frozenset({"unique_hit_multiplier"}),
    ),
}

BATCHES = (BATCH_1,)
