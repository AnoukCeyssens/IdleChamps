"""Champion Models: what each champion's effects do to a formation, for one Run Goal and Main DPS.

The one place that knows what champions do. Formation Search only sees this interface:

- `ChampionModels(catalogue, account)`
  - `loadouts(champion_id)`: the specialization and feat choices the search may pick between.
  - `scorer(layout, run_goal, main_dps)`: a `Scorer` for formations built around that Main DPS seat.
  - `status(champion_id)`: the champion's Model Status (Not Modelled unless listed in `RECONSTRUCTIONS`).
  - `assumptions(run_goal)`: the Scoring Assumptions every score for that goal rests on.
  - `explain(champion_id)`: the ChampionExplanation, what each gear slot, feat and specialization
    does at the player's gear in plain numbers (the champion page, #37).
- `Scorer.score(seats)`: the formation's `FormationScore` (log score, BUD, each seat's contribution).
- `Scorer.bound(seat)`: an optimistic log weight for one seat, never below what that seat adds to any
  formation with this Main DPS. The search builds its assignment and its gap from it.

This version is the generic layer (spec #22 build step 3): effect keys that are plain data
(`hero_dps_multiplier_mult`, `global_dps_multiplier_mult`, `gold_multiplier_mult`, `buff_upgrade(s)`)
with positional `targets`, from upgrades, chosen specializations, gear and feats. Everything else
(expressions, stacks, triggers, champion-unique keys) is skipped, so every champion is Not Modelled
and scored on its generic effects only. Per-champion reconstructions (#40) plug in behind the same
interface.
"""

from __future__ import annotations

import enum
import itertools
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache

from advisor.account import Account
from advisor.catalogue import Adventure, Catalogue, Slot

# Gold found grows with monster health to this power (research/push-area-reach.md §1.4: gold ≈
# HP^0.332), and a formation reaches monsters whose health matches its damage. So Favor per hour
# is compared as gold multipliers × Main DPS damage^0.332.
GOLD_PER_DAMAGE_EXPONENT = 0.332

# Gear effect = base at its rarity × gild × (1 + (item level − 1) / 250) (Emmotes' route planner,
# research/formation-strength-model.md §1.1).
_GILD_FACTOR = {0: 1.0, 1: 1.5, 2: 2.0}

_MAX_LOADOUTS = 64

# Breaking (Push only, #17/#33): an Armoured or Hits-based boss must lose all its segments within
# this many seconds. A formation that can't is penalised by this many log units per e-fold of
# missing breaking rate, so the search trades damage for Breakers where bosses need them.
BREAK_TIME_S = 120.0
BREAKING_WEIGHT = 10.0
DEFAULT_COOLDOWN_S = 5.0  # an attack whose definition isn't loaded

# Generic Breaker keys: extra segments per hit (and on which boss kinds), and hit multipliers.
_BREAK_EXTRA_KEYS = {
    "increase_damage_against_monster_armor": frozenset({"armoured"}),
    "increase_damage_against_monster_hits": frozenset({"hits"}),
    "increase_damage_against_monster_armor_and_hits": frozenset({"armoured", "hits"}),
    "increase_damage_on_armor_and_hits": frozenset({"armoured", "hits"}),
    "increase_num_unique_hits": frozenset({"armoured", "hits"}),
}
_BREAK_MULT_KEYS = {"unique_hit_multiplier": frozenset({"armoured", "hits"})}
# --- Gem Farm run model (spec #22: gems per hour from speed, stacking time, reset area, run length) ---
# Monster health grows by this factor per area (research/push-area-reach.md §1.2: core campaigns 2.031,
# events 1.85; Avernus, Rime and Vecna differ slightly and use the core value here).
CORE_HEALTH_GROWTH = 2.031
EVENT_HEALTH_GROWTH = 1.85
DEFAULT_GEM_FARM_ANCHOR = 50  # no best area and no objective: assume area 50
GEMS_PER_BOSS = 2.0  # the recording: 161 gems from 83 bosses this reset
AREA_SECONDS = 10.0  # an area the formation kills at once: walking and spawns
KILL_SECONDS = 10.0  # extra time at the one-shot area, growing with monster health past it
RESET_SECONDS = 30.0  # the modron reset and reload, per run
STACKING_SECONDS = 0.0  # no stack-based effect is modelled yet (Briv's reconstruction, #40, adds it)
RAMP_SECONDS = 30.0  # levelling the formation again after each reset
FIELD_FAMILIAR_SECONDS = 5.0  # ramp time saved per familiar on the field (it levels its champion)
CLICK_FAMILIAR_SHARE = 0.10  # each familiar on clicks kills 10% faster where the formation can't one-shot
MAX_RESET_AREA = 2500  # game rule `max_modron_auto_reset_area`

GEM_FARM_ASSUMPTIONS = (
    "Gems per hour = gems per run ÷ run time, about 2 gems per boss (every 5th area up to the reset area).",
    "Stacks are taken at their expected count at the reset area; random effects at their expected value. "
    "Briv's skips are not modelled yet (generic effects only): every area is walked.",
    "Your best area on this adventure is where the Main DPS alone stops killing at once; every ×2.031 of "
    "buffs to it (×1.85 on events) moves that one area further. Past it, each area takes longer by the "
    "monster health growth.",
    f"An area the formation kills at once takes {AREA_SECONDS:.0f} s; each run adds {RESET_SECONDS:.0f} s for "
    f"the modron reset, {STACKING_SECONDS:.0f} s of stacking time and a {RAMP_SECONDS:.0f} s levelling ramp.",
    f"Each familiar on the field levels its champion after a reset, cutting the ramp by "
    f"{FIELD_FAMILIAR_SECONDS:.0f} s; each familiar on clicks kills {CLICK_FAMILIAR_SHARE:.0%} faster past the "
    "one-shot area. Clicks and familiars don't count as hits for breaking.",
    "Armoured and hits-based monsters (breaking) are ignored for Gem Farm.",
    "No game speed effects are modelled yet (speed champions are Not Modelled): game speed ×1.",
    "Modron node bonuses are not modelled (no tile definitions): the core with the most experience is used, "
    "with its saved node layout.",
)
_SETUP_ASSUMPTIONS = (
    "Familiars and the modron core don't change this score: familiars go on the field on the Main DPS and its "
    "biggest supporters, any left over on clicks; the modron core keeps its saved reset area.",
)


class ModelStatus(enum.Enum):
    NOT_MODELLED = "Not Modelled"
    VERIFIED = "Verified"
    CHECK_FAILED = "Check Failed"
    STALE = "Stale"


@dataclass(frozen=True)
class Reconstruction:
    """A champion's hand-built model of its unique effects (#40). `description_tests`: the test ids
    that check it against the champion's description; the suite keeps them passing."""

    description_tests: tuple[str, ...] = ()


# Champion id -> its reconstruction. Champions not listed are Not Modelled (generic effects only).
RECONSTRUCTIONS: dict[int, Reconstruction] = {}


@dataclass(frozen=True)
class Loadout:
    """The choices the player makes per champion: specialization option ids and equipped feat ids."""

    specializations: tuple[int, ...] = ()
    feats: tuple[int, ...] = ()


@dataclass(frozen=True)
class Seat:
    """One champion in one formation slot, with its loadout."""

    champion_id: int
    slot: int
    loadout: Loadout = Loadout()


@dataclass(frozen=True)
class Contribution:
    """What one seat adds: the % it adds to the Main DPS's damage and to gold found."""

    damage_pct: float = 0.0
    gold_pct: float = 0.0


@dataclass(frozen=True)
class FormationScore:
    value: float  # log of the Run Goal's score; compare with `>`
    bud: float  # the Main DPS's biggest hit under this formation (model units)
    contributions: Mapping[int, Contribution] = field(default_factory=dict)  # by champion id
    reset_area: int | None = None  # Gem Farm: the modron reset area the score was taken at


@dataclass(frozen=True)
class BreakingNeed:
    """A boss the Push formation must break: Armoured ("armoured") or Hits-based ("hits"), with its
    segment count."""

    kind: str
    segments: float


@dataclass(frozen=True)
class _BreakEffect:
    op: str  # "extra" (segments added per hit) | "mult" (hits multiplied by 1 + amount / 100)
    amount: float
    kinds: frozenset[str]
    targets: tuple


@dataclass(frozen=True)
class _Effect:
    kind: str  # "damage" | "gold"
    log: float  # log of the multiplier
    targets: tuple  # "self", "all", "adj", ... or ("hero", id)


# Keys the generic layer understands, and the effect kind they change.
_DAMAGE_KEYS = {"hero_dps_multiplier_mult": "self", "global_dps_multiplier_mult": "all"}
_GOLD_KEYS = {"gold_multiplier_mult"}
_BUFF_KEYS = {"buff_upgrade", "buff_upgrades"}
# Effect-key fields that make an effect more than plain data (expressions, stacks, triggers, filters).
_COMPLEX_FIELDS = {
    "amount_expr", "per_hero_expr", "post_process_expr", "stack_func", "stacks_on_trigger", "more_triggers",
    "expression_on_trigger", "max_stacks", "filter_targets", "target_filters", "amount_func", "stacks_multiply",
    "pre_stack", "pre_stack_amount", "time_stack_type", "area_change_resets_stacks",
}
_TARGETS = {
    "self", "all", "adj", "behind", "ahead", "col", "next_col", "prev_col", "prev_and_next_col",
    "next_two_col", "prev_two_col", "front_column", "non_adj", "bud_setter",
}


@dataclass(frozen=True)
class EffectLine:
    """One effect of a gear item, feat or specialization, as the model sees it for this account.
    Exactly one of `modelled` (plain numbers at the player's gear, e.g. "+121% damage to all
    champions"), `assumption` (the Scoring Assumption an uncomputable effect is scored under; filled
    by per-champion reconstructions, #40) or `not_modelled` (why it isn't scored) is set."""

    modelled: str | None = None
    assumption: str | None = None
    not_modelled: str | None = None


@dataclass(frozen=True)
class ChampionExplanation:
    """Champion Models' second interface: what this champion's choices do for this account."""

    champion_id: int
    gear: Mapping[int, tuple[EffectLine, ...]]  # by slot, at the owned item's rarity, level and gild
    feats: Mapping[int, tuple[EffectLine, ...]]  # by feat id (owned or not)
    specializations: Mapping[int, tuple[EffectLine, ...]]  # by option (upgrade) id
    gear_scaling: str = (
        "Gear effects are taken at the item's rarity × gild (shiny ×1.5, golden ×2) × (1 + (item level − 1) ÷ 250)."
    )


class ChampionModels:
    def __init__(
        self, catalogue: Catalogue, account: Account, *, potions: Sequence[str] = (),
        statuses: Mapping[int, ModelStatus] | None = None,
    ) -> None:
        """`potions`: effect strings of potions in use. Only hints pass any (#34); the baseline has none.
        `statuses`: the Model Status of each reconstructed champion, as validation left it
        (`advisor.model_status`); a reconstructed champion missing from it counts as Stale."""
        self.catalogue = catalogue
        self.account = account
        self.potions = tuple(potions)
        self.statuses = dict(statuses or {})
        self._effects = cache(self._build_effects)
        self._break_effects = cache(self._build_break_effects)
        self.break_ceiling = cache(self._build_break_ceiling)
        self._potion_effects = cache(self._build_potion_effects)

    # --- interface ---------------------------------------------------------------------------

    def status(self, champion_id: int) -> ModelStatus:
        """Not Modelled unless the champion has a reconstruction; then what validation says."""
        if champion_id not in RECONSTRUCTIONS:
            return ModelStatus.NOT_MODELLED
        return self.statuses.get(champion_id, ModelStatus.STALE)

    def reconstruction(self, champion_id: int) -> Reconstruction | None:
        return RECONSTRUCTIONS.get(champion_id)

    def loadouts(self, champion_id: int) -> tuple[Loadout, ...]:
        """Every specialization combination × every useful feat set. Feats are only chosen among owned
        feats whose effects the model can score; otherwise the equipped feats are kept."""
        choices = self.catalogue.specializations.get(champion_id, ())
        spec_combos = list(itertools.islice(itertools.product(*[[o.id for o in c.options] for c in choices]), _MAX_LOADOUTS))
        owned = self.account.champions.get(champion_id)
        feat_sets: list[tuple[int, ...]] = [tuple(sorted(owned.equipped_feats)) if owned else ()]
        if owned:
            scorable = [f for f in owned.feats if self._feat_is_scorable(f)]
            if scorable:
                size = min(owned.feat_slots, len(scorable))
                feat_sets = [tuple(sorted(c)) for c in itertools.combinations(scorable, size)]
        combos = itertools.product(spec_combos or [()], feat_sets)
        return tuple(Loadout(tuple(s), f) for s, f in itertools.islice(combos, _MAX_LOADOUTS))

    def scorer(
        self, layout: Sequence[Slot], run_goal: str, main_dps: Seat, *, adventure: Adventure | None = None,
        breaking: Sequence[BreakingNeed] = (),
    ) -> Scorer:
        """`breaking`: the Armoured and Hits-based bosses a Push formation must break (Push only)."""
        gem_farm = self._gem_farm(adventure) if run_goal == "Gem Farm" else None
        return Scorer(self, {s.index: s for s in layout}, run_goal, main_dps, gem_farm,
                      tuple(breaking) if run_goal == "Push" else ())

    def _gem_farm(self, adventure: Adventure | None) -> GemFarm:
        """The run model for this adventure: anchored on the player's best area there."""
        anchor = None
        if adventure is not None:
            anchor = self.account.best_areas.get(adventure.id)
            if anchor is None and adventure.base_id is not None:
                anchor = self.account.best_areas.get(adventure.base_id)
            anchor = anchor or adventure.objective_area
        campaign = self.catalogue.campaign(adventure.campaign_id) if adventure is not None else None
        growth = EVENT_HEALTH_GROWTH if campaign is not None and campaign.is_event else CORE_HEALTH_GROWTH
        return GemFarm(anchor or DEFAULT_GEM_FARM_ANCHOR, growth)

    def assumptions(self, run_goal: str) -> tuple[str, ...]:
        common = (
            "All upgrades are bought; specializations and feats as recommended.",
            "Not Modelled champions are scored on their generic effects only (unique abilities, stacks and "
            "expressions are not counted).",
            "No potions.",
        )
        if run_goal == "Gold Farm":
            return (
                "Favor per hour is compared as gold multipliers × Main DPS damage^0.332 (gold found grows with "
                "monster health to the power 0.332): a relative score, not an absolute rate.",
                "Active player: click damage and click-based effects assume regular clicking.",
                "Daily-cap effects (gold and chest scavenging) are taken at their full daily cap, collected by an "
                "active player.",
                "Armoured and hits-based monsters (breaking) are ignored for Gold Farm.",
                *_SETUP_ASSUMPTIONS,
                *common,
            )
        if run_goal == "Push":
            return (
                "Push is scored on Main DPS damage, with stacking effects at max_stacks (Not Modelled champions' "
                "stacks are not counted yet).",
                *_SETUP_ASSUMPTIONS,
                *common,
            )
        if run_goal == "Gem Farm":
            return (*GEM_FARM_ASSUMPTIONS, *common)
        return (*_SETUP_ASSUMPTIONS, *common)

    def explain(self, champion_id: int) -> ChampionExplanation:
        """Each gear slot, feat and specialization option with its effects at the player's current gear,
        feats and specializations (buffs to an ability included)."""
        owned = self.account.champions.get(champion_id)
        current = Loadout(
            tuple(owned.specialization_choices) if owned else (), tuple(sorted(owned.equipped_feats)) if owned else ()
        )
        buffs = _buffs(self._sources(champion_id, current))
        gear = {}
        for slot, held in (owned.gear if owned else {}).items():
            item = self.catalogue.gear_item(champion_id, slot, held.rarity)
            if item is None:
                gear[slot] = (EffectLine(not_modelled="Not modelled: no item definition loaded for this slot."),)
                continue
            scale = _GILD_FACTOR.get(held.gild, 1.0) * (1 + (held.level - 1) / 250)
            gear[slot] = tuple(self._line(champion_id, _scaled(e, scale), {}, None, buffs) for e in item.effects)
        feat_ids = [*(owned.feats if owned else ()), *(f.id for f in self.catalogue.feats_of(champion_id))]
        feats = {}
        for feat_id in dict.fromkeys(feat_ids):
            feat = self.catalogue.feats.get(feat_id)
            feats[feat_id] = (
                tuple(self._line(champion_id, e, {}, None, buffs) for e in feat.effects) if feat
                else (EffectLine(not_modelled="Not modelled: no feat definition loaded."),)
            )
        specs = {}
        for choice in self.catalogue.specializations.get(champion_id, ()):
            for option in choice.options:
                specs[option.id] = self._upgrade_lines(champion_id, option.id, buffs)
        return ChampionExplanation(champion_id, gear, feats, specs)

    def _upgrade_lines(self, champion_id: int, upgrade_id: int, buffs: Mapping[int, float]) -> tuple[EffectLine, ...]:
        upgrade = self.catalogue.upgrades.get(upgrade_id)
        if upgrade is None:
            return (EffectLine(not_modelled="Not modelled: no upgrade definition loaded."),)
        if _key(upgrade.effect) != "effect_def":
            return (self._line(champion_id, upgrade.effect, {}, upgrade_id, buffs),)
        ability = self.catalogue.effects.get(_int(_params(upgrade.effect), 0))
        if ability is None:
            return (EffectLine(not_modelled="Not modelled: the ability's definition isn't loaded."),)
        return tuple(
            self._line(champion_id, str(k.get("effect_string") or ""), k, upgrade_id, buffs) for k in ability.effect_keys
        )

    def _line(
        self, champion_id: int, effect: str, fields: Mapping, upgrade_id: int | None, buffs: Mapping[int, float]
    ) -> EffectLine:
        """One effect string in plain numbers, as the generic layer scores it, or why it isn't scored."""
        key, params = _key(effect), _params(effect)
        breaker = key in _BREAK_EXTRA_KEYS or key in _BREAK_MULT_KEYS
        if not (key in _DAMAGE_KEYS or key in _GOLD_KEYS or key in _BUFF_KEYS or breaker):
            return EffectLine(not_modelled=f"Not modelled: {key or 'an empty effect'} is not a generic effect.")
        if complex_fields := sorted(_COMPLEX_FIELDS & set(fields)):
            return EffectLine(not_modelled=f"Not modelled: {key} depends on {', '.join(complex_fields)}.")
        pct = _float(params, 0)
        if key in _BUFF_KEYS:
            names = [self._upgrade_name(u) for p in params[1:] if (u := _maybe_int(p)) is not None]
            if pct is None or not names:
                return EffectLine(not_modelled=f"Not modelled: {effect} isn't plain data.")
            return EffectLine(modelled=f"+{_num(pct)}% to {' and '.join(names)}")
        if breaker:
            if pct is None and key == "increase_num_unique_hits":
                pct = 1.0
            raw = fields.get("targets")
            if raw is None:
                targets = ("all",) if key in _BREAK_MULT_KEYS else ("self",)
            else:
                targets = tuple(raw if isinstance(raw, list) else [raw])
            if pct is None or pct <= 0 or not all(isinstance(t, str) and t in _TARGETS for t in targets):
                return EffectLine(not_modelled=f"Not modelled: {effect} isn't plain data.")
            kinds = (_BREAK_EXTRA_KEYS | _BREAK_MULT_KEYS)[key]
            against = " and ".join(_BOSS_KINDS[k] for k in sorted(kinds))
            who = self._targets_text(champion_id, targets)
            if key in _BREAK_MULT_KEYS:
                return EffectLine(modelled=f"+{_num(pct)}% hits by {who} against {against} monsters")
            return EffectLine(modelled=f"+{_num(pct)} segments per hit by {who} against {against} monsters")
        if pct is None:
            return EffectLine(not_modelled=f"Not modelled: {effect} has no plain amount.")
        if upgrade_id is not None:
            pct *= 1 + buffs.get(upgrade_id, 0.0) / 100
        targets = _targets(key, params, fields)
        if targets is None:
            return EffectLine(not_modelled=f"Not modelled: {key}'s targeting isn't plain data.")
        if key in _GOLD_KEYS:
            return EffectLine(modelled=f"+{_num(pct)}% gold")
        return EffectLine(modelled=f"+{_num(pct)}% damage to {self._targets_text(champion_id, targets)}")

    def _upgrade_name(self, upgrade_id: int) -> str:
        upgrade = self.catalogue.upgrades.get(upgrade_id)
        if upgrade is None:
            return f"upgrade {upgrade_id} (definition not loaded)"
        return upgrade.name or f"upgrade {upgrade_id}"

    def _targets_text(self, champion_id: int, targets: tuple) -> str:
        names = []
        for target in targets:
            if isinstance(target, tuple):
                hero = self.catalogue.find(target[1])
                names.append(hero.name if hero else f"champion {target[1]}")
            elif target == "self":
                hero = self.catalogue.find(champion_id)
                names.append(hero.name if hero else "itself")
            else:
                names.append(_TARGET_TEXT.get(target, target))
        return " and ".join(names)

    # --- generic effect layer ----------------------------------------------------------------

    def effects(self, champion_id: int, loadout: Loadout) -> tuple[_Effect, ...]:
        return self._effects(champion_id, loadout)

    def base_damage(self, champion_id: int) -> float:
        champion = self.catalogue.find(champion_id)
        return champion.base_damage if champion and champion.base_damage > 0 else 1.0

    def break_effects(self, champion_id: int, loadout: Loadout) -> tuple[_BreakEffect, ...]:
        return self._break_effects(champion_id, loadout)

    def hit_rates(self, champion_id: int) -> tuple[float, float, float | None]:
        """(base attack hits per second on one boss, ultimate hits per second when it is used once
        within `BREAK_TIME_S`, the ultimate's damage per hit as a fraction of BUD). Multi-target
        attacks count as one hit on the boss; damage-over-time ticks and multi-hit fields count as
        hits. Clicks and familiars don't count."""
        champion = self.catalogue.find(champion_id)
        base_id = champion.base_attack_id if champion else None
        base = self.catalogue.attacks.get(base_id) if base_id else None
        cooldown = (base.cooldown if base else None) or self.catalogue.attack_cooldowns.get(base_id) or DEFAULT_COOLDOWN_S
        per_s = (base.hits_per_use if base else 1.0) / cooldown
        ult_id = champion.ultimate_attack_id if champion else None
        ult = self.catalogue.attacks.get(ult_id) if ult_id else None
        return per_s, (ult.hits_per_use / BREAK_TIME_S if ult else 0.0), (ult.damage_modifier if ult else None)

    def _build_break_effects(self, champion_id: int, loadout: Loadout) -> tuple[_BreakEffect, ...]:
        found = []
        for _, effect, fields in self._sources(champion_id, loadout):
            key = _key(effect)
            if key not in _BREAK_EXTRA_KEYS and key not in _BREAK_MULT_KEYS:
                continue
            if _COMPLEX_FIELDS & set(fields):
                continue  # conditional Breaker effects belong to per-champion reconstructions
            amount = _float(_params(effect), 0)
            if amount is None and key == "increase_num_unique_hits":
                amount = 1.0
            raw = fields.get("targets")
            if raw is None:
                targets = ("all",) if key in _BREAK_MULT_KEYS else ("self",)
            else:
                targets = tuple(raw if isinstance(raw, list) else [raw])
            if amount is None or amount <= 0 or not all(isinstance(t, str) and t in _TARGETS for t in targets):
                continue
            if key in _BREAK_MULT_KEYS:
                found.append(_BreakEffect("mult", amount, _BREAK_MULT_KEYS[key], targets))
            else:
                found.append(_BreakEffect("extra", amount, _BREAK_EXTRA_KEYS[key], targets))
        return tuple(found)

    def _build_break_ceiling(self, kind: str) -> tuple[float, float]:
        """(most extra segments per hit, largest hit multiplier) any formation of owned champions could
        give: the sum and product over every owned champion's best loadout. Keeps `Scorer.bound` honest."""
        extra, mult = 0.0, 1.0
        for champion_id in self.account.champions:
            if self.catalogue.find(champion_id) is None:
                continue
            best_extra, best_mult = 0.0, 1.0
            for loadout in self.loadouts(champion_id):
                effects = [e for e in self.break_effects(champion_id, loadout) if kind in e.kinds]
                best_extra = max(best_extra, sum(e.amount for e in effects if e.op == "extra"))
                best_mult = max(best_mult, math.prod(1 + e.amount / 100 for e in effects if e.op == "mult"))
            extra += best_extra
            mult *= best_mult
        return extra, mult

    def potion_effects(self) -> tuple[_Effect, ...]:
        """The potions' generic effects; they reach every champion, so the Main DPS."""
        return self._potion_effects()

    def _build_potion_effects(self) -> tuple[_Effect, ...]:
        found = []
        for effect in self.potions:
            key, pct = _key(effect), _float(_params(effect), 0)
            if pct is not None and (key in _DAMAGE_KEYS or key in _GOLD_KEYS):
                found.append(_Effect("gold" if key in _GOLD_KEYS else "damage", math.log(max(1e-9, 1 + pct / 100)), ("all",)))
        return tuple(found)

    def _feat_is_scorable(self, feat_id: int) -> bool:
        feat = self.catalogue.feats.get(feat_id)
        return bool(feat and any(_key(e) in _DAMAGE_KEYS or _key(e) in _GOLD_KEYS for e in feat.effects))

    def _sources(self, champion_id: int, loadout: Loadout) -> list[tuple[int | None, str, Mapping]]:
        """(upgrade id, effect string, effect-key fields) of every effect the loadout has."""
        sources: list[tuple[int | None, str, Mapping]] = []
        chosen = set(loadout.specializations)
        for upgrade in self.catalogue.upgrades_of(champion_id):
            if upgrade.is_specialization and upgrade.id not in chosen:
                continue
            if _key(upgrade.effect) == "effect_def":
                ability = self.catalogue.effects.get(_int(_params(upgrade.effect), 0))
                for key in ability.effect_keys if ability else ():
                    sources.append((upgrade.id, str(key.get("effect_string") or ""), key))
            else:
                sources.append((upgrade.id, upgrade.effect, {}))
        owned = self.account.champions.get(champion_id)
        for slot, gear in (owned.gear if owned else {}).items():
            item = self.catalogue.gear_item(champion_id, slot, gear.rarity)
            scale = _GILD_FACTOR.get(gear.gild, 1.0) * (1 + (gear.level - 1) / 250)
            for effect in item.effects if item else ():
                sources.append((None, _scaled(effect, scale), {}))
        for feat_id in loadout.feats:
            feat = self.catalogue.feats.get(feat_id)
            for effect in feat.effects if feat else ():
                sources.append((None, effect, {}))
        return sources

    def _build_effects(self, champion_id: int, loadout: Loadout) -> tuple[_Effect, ...]:
        sources = self._sources(champion_id, loadout)
        buffs = _buffs(sources)

        effects = []
        for upgrade_id, effect, fields in sources:
            key = _key(effect)
            if key not in _DAMAGE_KEYS and key not in _GOLD_KEYS:
                continue  # buffs were applied above; neutral and unknown keys change no score here
            if _COMPLEX_FIELDS & set(fields):
                continue
            params = _params(effect)
            pct = _float(params, 0)
            if pct is None:
                continue
            pct *= 1 + buffs.get(upgrade_id, 0.0) / 100 if upgrade_id is not None else 1
            targets = _targets(key, params, fields)
            if targets is None:
                continue
            effects.append(_Effect("gold" if key in _GOLD_KEYS else "damage", math.log(max(1e-9, 1 + pct / 100)), targets))
        return tuple(effects)


class Scorer:
    """Scores formations built around one Main DPS seat, for one layout and Run Goal.

    `score(seats)` is the placement score: additive over seats, bounded by `bound`. `score(seats, setup)`
    adds the familiars and modron core (`formation_search.Setup`): for Gem Farm that turns the Main DPS
    damage into gems per hour; `lift(value, setup)` does the same for any placement score (it is
    increasing in `value` and ignores which slots hold the familiars, so it lifts bounds too). Familiars
    and cores don't change the Push and Gold Farm scores.
    """

    def __init__(
        self, models: ChampionModels, slots: Mapping[int, Slot], run_goal: str, main_dps: Seat,
        gem_farm: GemFarm | None = None, breaking: tuple[BreakingNeed, ...] = (),
    ) -> None:
        self.models = models
        self.slots = slots
        self.run_goal = run_goal
        self.main_dps = main_dps
        self.breaking = breaking
        self.gem_farm = gem_farm
        self._damage_weight = GOLD_PER_DAMAGE_EXPONENT if run_goal == "Gold Farm" else 1.0
        self._gold_weight = 1.0 if run_goal == "Gold Farm" else 0.0
        self._bounds: dict[Seat, float] = {}
        self._last: tuple[tuple[Seat, ...], FormationScore] | None = None

    def lift(self, value: float, setup=None) -> float:
        return self._lift(value, setup)[0]

    def _lift(self, value: float, setup) -> tuple[float, int | None]:
        if self.gem_farm is None or setup is None:
            return value, None
        on_field = sum(1 for f in setup.familiars if f is not None)
        multiplier = value - math.log(self.models.base_damage(self.main_dps.champion_id))
        return self.gem_farm.rate(multiplier, on_field, len(setup.familiars) - on_field)

    def score(self, seats: Iterable[Seat], setup=None) -> FormationScore:
        seats = tuple(seats)
        if setup is None:
            return self._score(seats)
        if self._last is None or self._last[0] != seats:  # setups are tried in turn on the same seats
            self._last = (seats, self._score(seats))
        placement = self._last[1]
        value, reset_area = self._lift(placement.value, setup)
        return FormationScore(value, placement.bud, placement.contributions, reset_area)

    def _score(self, seats: Iterable[Seat]) -> FormationScore:
        seats = tuple(seats)
        if self.main_dps not in seats:
            seats = (self.main_dps, *seats)
        damage_log = math.log(self.models.base_damage(self.main_dps.champion_id))
        gold_log = 0.0
        for effect in self.models.potion_effects():
            damage_log += effect.log if effect.kind == "damage" else 0.0
            gold_log += effect.log if effect.kind == "gold" else 0.0
        contributions = {}
        for seat in seats:
            damage, gold = self._reach(seat)
            damage_log += damage
            gold_log += gold
            contributions[seat.champion_id] = Contribution(_pct(damage), _pct(gold))
        value = self._damage_weight * damage_log + self._gold_weight * gold_log
        if self.breaking:
            rates = {kind: self.breaking_rate(seats, kind) for kind in {n.kind for n in self.breaking}}
            value += BREAKING_WEIGHT * sum(
                min(0.0, math.log(rates[n.kind] * BREAK_TIME_S / n.segments)) for n in self.breaking
            )
        return FormationScore(value=value, bud=math.exp(min(damage_log, 700)), contributions=contributions)

    def breaking_rate(
        self, seats: Iterable[Seat], kind: str, *, main_qualifies: bool = True, ult_min_modifier: float | None = None
    ) -> float:
        """Segments per second the formation breaks on one boss of `kind` ("armoured" | "hits").

        Hits-based: every champion's hits count. Armoured: only hits at or above the chunk's health
        count: the Main DPS's (when `main_qualifies`) and ultimates whose damage per hit is at least
        `ult_min_modifier` × BUD (None: every ultimate). Generic Breaker effects reaching a champion
        add segments per hit or multiply its hits."""
        seats = tuple(seats)
        if self.main_dps not in seats:
            seats = (self.main_dps, *seats)
        effects = [
            (seat, e) for seat in seats for e in self.models.break_effects(seat.champion_id, seat.loadout)
            if kind in e.kinds
        ]
        rate = 0.0
        for seat in seats:
            per_s, ult, ult_modifier = self.models.hit_rates(seat.champion_id)
            is_main = seat.champion_id == self.main_dps.champion_id
            hits = per_s if kind == "hits" or (is_main and main_qualifies) else 0.0
            if ult_min_modifier is None or (ult_modifier is not None and ult_modifier >= ult_min_modifier):
                hits += ult
            if not hits:
                continue
            extra, mult = 0.0, 1.0
            for source, effect in effects:
                if _lands(self.slots, effect.targets, source, seat, is_main):
                    if effect.op == "extra":
                        extra += effect.amount
                    else:
                        mult *= 1 + effect.amount / 100
            rate += hits * (1 + extra) * mult
        return rate

    def bound(self, seat: Seat) -> float:
        """Generic effects are separable: a seat adds exactly its own reach, so the bound is exact."""
        if seat not in self._bounds:
            damage, gold = self._reach(seat)
            self._bounds[seat] = self._damage_weight * damage + self._gold_weight * gold + self._breaking_bound(seat)
        return self._bounds[seat]

    def _breaking_bound(self, seat: Seat) -> float:
        """Most a seat can lift the breaking penalty. Each need's term is min(0, log rate), and adding a
        seat multiplies the rate by at most (1 + its own hits at the best possible extras and
        multipliers ÷ the Main DPS's bare hits) × (1 + each extra it gives) × (each multiplier it gives),
        and never by more than the Main DPS alone falls short."""
        if not self.breaking:
            return 0.0
        floor = self.models.hit_rates(self.main_dps.champion_id)[0]
        per_s, ult, _ = self.models.hit_rates(seat.champion_id)
        total = 0.0
        for need in self.breaking:
            extra, mult = self.models.break_ceiling(need.kind)
            own = (per_s if need.kind == "hits" else 0.0) + ult
            log_gain = math.log1p(own * (1 + extra) * mult / floor)
            for e in self.models.break_effects(seat.champion_id, seat.loadout):
                if need.kind in e.kinds:
                    log_gain += math.log1p(e.amount) if e.op == "extra" else math.log1p(e.amount / 100)
            # Seats only ever raise the rate, so no seat lifts more than the Main DPS alone is missing.
            alone = self.breaking_rate((), need.kind)
            missing = -min(0.0, math.log(alone * BREAK_TIME_S / need.segments)) if alone > 0 else math.inf
            total += BREAKING_WEIGHT * min(log_gain, missing)
        return total

    def _reach(self, seat: Seat) -> tuple[float, float]:
        """Log damage multiplier this seat gives the Main DPS, and log gold multiplier."""
        damage = gold = 0.0
        for effect in self.models.effects(seat.champion_id, seat.loadout):
            if effect.kind == "gold":
                gold += effect.log
            elif self._reaches(effect.targets, seat):
                damage += effect.log
        return damage, gold

    def _reaches(self, targets: tuple, source: Seat) -> bool:
        return _lands(self.slots, targets, source, self.main_dps)


def _lands(slots: Mapping[int, Slot], targets: tuple, source: Seat, dest: Seat, dest_is_main: bool = True) -> bool:
    """Does an effect of `source` with these targets reach the champion in seat `dest`?"""
    is_self = source.champion_id == dest.champion_id
    src, dst = slots.get(source.slot), slots.get(dest.slot)
    for target in targets:
        if isinstance(target, tuple):
            if target[1] == dest.champion_id:
                return True
            continue
        if target == "all" or (target == "bud_setter" and dest_is_main) or (target == "self" and is_self):
            return True
        if is_self or src is None or dst is None:
            continue
        adjacent = dst.index in src.adjacent
        column_step = dst.column - src.column
        if (
            (target == "adj" and adjacent)
            or (target == "non_adj" and not adjacent)
            or (target == "behind" and adjacent and column_step == 1)
            or (target == "ahead" and adjacent and column_step == -1)
            or (target == "col" and column_step == 0)
            or (target == "next_col" and column_step == 1)
            or (target == "prev_col" and column_step == -1)
            or (target == "prev_and_next_col" and abs(column_step) == 1)
            or (target == "next_two_col" and column_step in (1, 2))
            or (target == "prev_two_col" and column_step in (-1, -2))
            or (target == "front_column" and dst.column == min(s.column for s in slots.values()))
        ):
            return True
    return False


@dataclass(frozen=True)
class GemFarm:
    """One adventure's gem farm loop: run from area 1 to the reset area, reset, repeat."""

    anchor: float  # area where the Main DPS alone stops killing at once
    growth: float  # monster health per area

    def rate(self, damage_log: float, on_field: int, clicks: int) -> tuple[float, int]:
        """(log gems per hour, best reset area) for a formation multiplying the Main DPS by e^damage_log."""
        return _gem_rate(round(self.anchor + damage_log / math.log(self.growth), 9), self.growth, on_field, clicks)


@cache
def _gem_rate(one_shot: float, growth: float, on_field: int, clicks: int) -> tuple[float, int]:
    overhead = RESET_SECONDS + STACKING_SECONDS + max(0.0, RAMP_SECONDS - FIELD_FAMILIAR_SECONDS * on_field)
    kill = KILL_SECONDS / (1 + CLICK_FAMILIAR_SHARE * clicks)
    log_g = math.log(growth)
    last = min(MAX_RESET_AREA, max(5, 5 * math.ceil((one_shot + 60) / 5)))
    best = (-math.inf, 5)
    for reset in range(5, last + 1, 5):
        # Σ_{a=1..R} g^(a − one_shot), as a geometric sum.
        slow = (math.exp((reset + 1 - one_shot) * log_g) - math.exp((1 - one_shot) * log_g)) / (growth - 1)
        seconds = overhead + reset * AREA_SECONDS + kill * slow
        value = math.log(3600 * GEMS_PER_BOSS * (reset // 5) / seconds)
        if value > best[0] + 1e-12:
            best = (value, reset)
    return best


def _buffs(sources: Iterable[tuple[int | None, str, Mapping]]) -> dict[int, float]:
    """Upgrade id -> % its effect is raised by (`buff_upgrade(s)` among the sources)."""
    buffs: dict[int, float] = defaultdict(float)
    for _, effect, _fields in sources:
        if _key(effect) in _BUFF_KEYS:
            params = _params(effect)
            pct = _float(params, 0)
            for upgrade_id in params[1:]:
                if (u := _maybe_int(upgrade_id)) is not None and pct is not None:
                    buffs[u] += pct
    return buffs


_BOSS_KINDS = {"armoured": "Armoured", "hits": "Hits-based"}
_TARGET_TEXT = {
    "all": "all champions",
    "adj": "adjacent champions",
    "non_adj": "non-adjacent champions",
    "behind": "the adjacent champions behind",
    "ahead": "the adjacent champions ahead",
    "col": "champions in its column",
    "next_col": "champions in the column behind",
    "prev_col": "champions in the column ahead",
    "prev_and_next_col": "champions in the columns ahead and behind",
    "next_two_col": "champions in the two columns behind",
    "prev_two_col": "champions in the two columns ahead",
    "front_column": "champions in the front column",
    "bud_setter": "the Main DPS",
}


def _num(value: float) -> str:
    """A percentage in plain numbers: 120.96 → "121", 50.8 → "50.8"."""
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:.3g}"


def _pct(log: float) -> float:
    return (math.exp(min(log, 700)) - 1) * 100


def _key(effect: str) -> str:
    return effect.split(",", 1)[0].strip()


def _params(effect: str) -> list[str]:
    return [p.strip() for p in effect.split(",")[1:]]


def _maybe_int(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _int(params: list[str], index: int) -> int:
    return _maybe_int(params[index]) if index < len(params) else 0


def _float(params: list[str], index: int) -> float | None:
    try:
        return float(params[index])
    except (IndexError, ValueError):
        return None


def _scaled(effect: str, scale: float) -> str:
    """The effect string with its amount (first parameter) multiplied by `scale`."""
    key, params = _key(effect), _params(effect)
    amount = _float(params, 0)
    if amount is None:
        return effect
    return ",".join([key, repr(amount * scale), *params[1:]])


def _targets(key: str, params: list[str], fields: Mapping) -> tuple | None:
    """Where the effect lands, or None when the targeting isn't plain data."""
    raw = fields.get("targets")
    if raw is None:
        if key in _GOLD_KEYS:
            return ("all",)
        default = _DAMAGE_KEYS[key]
        # `hero_dps_multiplier_mult,20,153`: a third parameter names the champion it applies to.
        if default == "self" and len(params) > 1 and (hero := _maybe_int(params[1])) is not None:
            return (("hero", hero),)
        return (default,)
    raw = raw if isinstance(raw, list) else [raw]
    if not all(isinstance(t, str) and t in _TARGETS for t in raw):
        return None
    return tuple(raw)
