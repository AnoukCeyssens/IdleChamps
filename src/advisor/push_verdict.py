"""Push Verdict (spec #22 module 6, decided in #14/#16): will this Push formation reach the objective?

Damage-only, anchored relative estimate:

- **Anchor Run**: my live instance whose BUD was set by the same Main DPS (highest BUD among them),
  otherwise the instance with the highest BUD.
- **Damage**: the anchor's BUD × S(formation) / S(anchor formation), both scored by the Champion
  Models for Push, so account-wide unknowns cancel.
- **Damage wall**: the first boss area (×50 health) whose boss 30 s of Main DPS hits can't kill, on
  the campaign's health curve with the adventure's handled health modifiers. The expected area is
  the area before it, shown rounded to 5.
- **Band W**: 10 areas on core campaigns, 12 on events; +5 when the anchor's Main DPS differs; +3 per
  unverified champion in either formation. Likely / Borderline / Unlikely from the margin to the
  objective. Without known monster placement the verdict is capped at Borderline.
- **Facts win**: "Reached before (area N)" when the best area already meets a non-patron objective.

Breaking checks (Armoured and Hits-based bosses) need monster placement (the Area Set Cache, a later
ticket); until then every verdict is damage-only.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from advisor.account import Account, GameInstance
from advisor.catalogue import Adventure, Catalogue, Champion, HealthCurve
from advisor.champion_models import ChampionModels, Loadout, ModelStatus, Seat

KILL_TIME_S = 30.0  # a boss must die within this many seconds of Main DPS hits
DEFAULT_COOLDOWN_S = 5.0  # when the Main DPS's attack definition isn't loaded
BOSS_HEALTH = 50.0
MAX_AREA = 2500
BAND_CORE, BAND_EVENT, BAND_OTHER_MAIN_DPS, BAND_PER_UNVERIFIED = 10, 12, 5, 3

NO_ANCHOR = "No estimate: start any Push run and Refresh"
PLACEMENT_UNKNOWN = "armoured or hits-based bosses not checked (positions unknown)"

# Adventure modifiers (`game_changes` types) that change nothing a damage-only estimate reads.
_NEUTRAL = {
    "formation", "formation_saves_campaign_id", "initial_formation", "force_use_heroes", "force_allow_hero",
    "only_allow_crusaders", "disallow_crusaders", "unlocked_upgrades", "buff_display_by_area", "weather",
    "darken_by_area", "force_skin", "dodge_this_deaders_overlay", "gold_icons", "disallow_ultimates",
    "active_monster_limit", "quest_requirement_mult", "random_crusader_damage", "monster_base_stats",
}
# Modifiers that change which monster stands where: covered by the "positions unknown" cap.
_PLACEMENT = {
    "random_monster", "random_monster_waves", "guaranteed_monster", "guaranteed_monsters", "additional_bosses",
    "additional_bosses_by_area", "replace_monsters_by_area",
}
_ESCORTS = {"slot_escort", "slot_escort_by_area"}
_MONSTER_PROPERTIES = {"add_monster_properties", "add_monster_properties_by_tag", "add_monster_properties_by_id"}
# Monster properties that leave monster health alone (damage, speed, gold, quest counting).
_HARMLESS_PROPERTIES = {"damage_multiplier", "speed_multiplier", "gold_multiplier", "counts_for_quests"}
_HARMLESS_MONSTER_EFFECTS = {"monster_damage_increase", "monster_speed_increase", "set_monster_damage_to_percent"}


@dataclass(frozen=True)
class AnchorRun:
    instance: GameInstance
    main_dps: Champion
    adventure_name: str
    same_main_dps: bool


@dataclass(frozen=True)
class PushVerdict:
    kind: str  # "reached" | "likely" | "borderline" | "unlikely" | "estimate" (no objective) | "no_estimate" | "not_available"
    label: str  # what the Push tab shows, e.g. "Reached before (area 50)", "Borderline"
    objective: int | None
    best_area: int | None  # "best here", always shown
    expected_area: int | None = None  # rounded to 5
    area: int | None = None  # unrounded: the last area before the damage wall
    band: int | None = None  # W
    anchor: AnchorRun | None = None
    capped: bool = False  # Likely capped to Borderline (placement unknown)
    assumptions: tuple[str, ...] = ()


def push_verdict(
    catalogue: Catalogue,
    account: Account,
    models: ChampionModels,
    adventure: Adventure,
    patron: int | None,
    formation: Sequence[Seat],
    main_dps: Seat | None,
    *,
    placement_known: bool = False,
) -> PushVerdict:
    """The Push Verdict for `formation` (built around `main_dps`) on `adventure`, under `patron`."""
    objective = adventure.patron_objectives.get(patron) if patron is not None else adventure.objective_area
    best = account.best_areas.get(adventure.id)
    if patron is None and objective is not None and best is not None and best >= objective:
        return PushVerdict("reached", f"Reached before (area {best})", objective, best)
    reason, _ = _modifiers(adventure)
    if reason:
        return PushVerdict("not_available", f"Estimate not available: {reason}", objective, best)
    if main_dps is None:
        return PushVerdict("not_available", "Estimate not available: no legal formation", objective, best)
    anchor = choose_anchor(catalogue, account, main_dps.champion_id)
    if anchor is None:
        return PushVerdict("no_estimate", NO_ANCHOR, objective, best)
    return _estimate(catalogue, account, models, adventure, patron, formation, main_dps, anchor, objective, best,
                     placement_known)


def instance_estimate(catalogue: Catalogue, account: Account, models: ChampionModels, instance: GameInstance) -> PushVerdict | None:
    """The estimate for a live run's formation in play on its adventure (for the History Record and
    the Push calibration check), or None when there is none to make."""
    adventure = catalogue.adventure(instance.adventure_id)
    if adventure is None or instance.bud_champion_id is None or catalogue.find(instance.bud_champion_id) is None:
        return None
    seats = formation_in_play(catalogue, account, instance)
    main = next((s for s in seats if s.champion_id == instance.bud_champion_id), None)
    if main is None or _modifiers(adventure)[0]:
        return None
    anchor = choose_anchor(catalogue, account, main.champion_id)
    if anchor is None:
        return None
    objective = (
        adventure.patron_objectives.get(instance.patron_id) if instance.patron_id else adventure.objective_area
    )
    return _estimate(catalogue, account, models, adventure, instance.patron_id, seats, main, anchor, objective,
                     account.best_areas.get(adventure.id), False)


def choose_anchor(catalogue: Catalogue, account: Account, main_dps_id: int) -> AnchorRun | None:
    usable = [
        i for i in account.instances if i.bud and i.bud_champion_id is not None and catalogue.find(i.bud_champion_id)
    ]
    if not usable:
        return None
    same = [i for i in usable if i.bud_champion_id == main_dps_id]
    instance = max(same or usable, key=lambda i: i.bud)
    adventure = catalogue.adventure(instance.adventure_id)
    return AnchorRun(
        instance,
        catalogue.champion(instance.bud_champion_id),
        adventure.name if adventure else f"adventure {instance.adventure_id}",
        bool(same),
    )


def band_width(is_event: bool, same_main_dps: bool, unverified: int) -> int:
    return (BAND_EVENT if is_event else BAND_CORE) + (0 if same_main_dps else BAND_OTHER_MAIN_DPS) + BAND_PER_UNVERIFIED * unverified


def wall_area(curve: HealthCurve, log_damage: float, log_health_mult: float = 0.0) -> int:
    """The last area before the first boss that `exp(log_damage)` can't kill (MAX_AREA if none)."""
    log_health = math.log(curve.base_health) + math.log(BOSS_HEALTH) + log_health_mult
    for area in range(2, MAX_AREA + 1):
        log_health += math.log(_growth(curve, area))
        if area % 5 == 0 and log_health > log_damage:
            return area - 1
    return MAX_AREA


def _estimate(catalogue, account, models, adventure, patron, formation, main_dps, anchor, objective, best, placement_known):
    campaign = catalogue.campaign(adventure.campaign_id)
    curve = campaign.health_curve or catalogue.health_curve
    _, log_health_mult = _modifiers(adventure)

    s_formation = models.scorer(adventure.layout, "Push", main_dps).score(formation).value
    anchor_seats = formation_in_play(catalogue, account, anchor.instance)
    anchor_main = next(
        (s for s in anchor_seats if s.champion_id == anchor.main_dps.id),
        Seat(anchor.main_dps.id, -1, _loadout_in_play(account, anchor.main_dps.id)),
    )
    anchor_adventure = catalogue.adventure(anchor.instance.adventure_id)
    s_anchor = models.scorer(anchor_adventure.layout if anchor_adventure else (), "Push", anchor_main).score(anchor_seats).value

    main = catalogue.champion(main_dps.champion_id)
    cooldown = catalogue.attack_cooldowns.get(main.base_attack_id) if main.base_attack_id else None
    hits = max(1.0, KILL_TIME_S / (cooldown or DEFAULT_COOLDOWN_S))
    log_damage = math.log(anchor.instance.bud) + s_formation - s_anchor + math.log(hits)
    area = wall_area(curve, log_damage, log_health_mult)
    expected = int(5 * round(area / 5))

    ids = {s.champion_id for s in formation} | {s.champion_id for s in anchor_seats} | {main.id}
    unverified = sum(1 for i in ids if models.status(i) is not ModelStatus.VERIFIED)
    band = band_width(campaign.is_event, anchor.same_main_dps, unverified)

    capped = False
    if objective is None:
        kind, label = "estimate", "No objective (free play)"
    elif area - objective >= band:
        kind, label = "likely", "Likely"
        if not placement_known:
            kind, label, capped = "borderline", "Borderline", True
    elif area - objective < -band:
        kind, label = "unlikely", "Unlikely"
    else:
        kind, label = "borderline", "Borderline"

    instance = anchor.instance
    assumptions = [
        f"Anchor Run: instance {instance.id} on {anchor.adventure_name} (area {instance.current_area}), Main DPS "
        f"{anchor.main_dps.name}, BUD {_sci(instance.bud)}"
        + ("." if anchor.same_main_dps else f"; a different Main DPS than {main.name}, so the band is 5 wider."),
        "The estimate scales the Anchor Run's BUD by the two formations' relative Push scores; a parked or "
        "under-levelled Anchor Run makes it low.",
        f"Damage wall: a boss (×50 health) must die within {KILL_TIME_S:.0f} s of Main DPS hits "
        + (f"({main.name} attacks every {cooldown:g} s)." if cooldown else
           f"(attack cooldown not loaded, taken as {DEFAULT_COOLDOWN_S:g} s)."),
        f"Monster health ×{_growth(curve, 2):g} per area ({'event' if campaign.is_event else 'campaign'} curve)"
        + (f", ×{math.exp(log_health_mult):g} from the adventure's monster modifiers." if log_health_mult else "."),
        f"Band ±{band} areas: {BAND_EVENT if campaign.is_event else BAND_CORE} on "
        f"{'event' if campaign.is_event else 'core'} campaigns"
        + ("" if anchor.same_main_dps else f", +{BAND_OTHER_MAIN_DPS} for the anchor's different Main DPS")
        + (f", +{BAND_PER_UNVERIFIED} for each of {unverified} unverified champions" if unverified else "")
        + ".",
    ]
    if not placement_known:
        assumptions.append(f"{PLACEMENT_UNKNOWN[0].upper()}{PLACEMENT_UNKNOWN[1:]}: the verdict is at best Borderline.")
    if patron is not None:
        assumptions.append("Patron objectives are always estimated: the best area is shared with the base adventure.")
    return PushVerdict(kind, label, objective, best, expected, area, band, anchor, capped, tuple(assumptions))


def _modifiers(adventure: Adventure) -> tuple[str | None, float]:
    """(why the adventure can't be estimated, or None; log of the boss health multiplier it adds)."""
    reasons = {"timer": None, "escort": None, "other": None}
    log_mult = 0.0
    for change in adventure.game_changes:
        kind = str(change.get("type") or "")
        if kind in _NEUTRAL or kind in _PLACEMENT:
            continue
        if "timer" in kind or "time_limit" in kind:
            reasons["timer"] = reasons["timer"] or "timed adventure"
        elif kind in _ESCORTS:
            reasons["escort"] = reasons["escort"] or "escort adventure"
        elif kind in _MONSTER_PROPERTIES and (mult := _health_multiplier(change)) is not None:
            log_mult = max(log_mult, math.log(mult)) if mult > 0 else log_mult
        else:
            reasons["other"] = reasons["other"] or f"unhandled adventure modifier ({kind})"
    return reasons["timer"] or reasons["escort"] or reasons["other"], log_mult


def _health_multiplier(change: dict) -> float | None:
    """The boss health multiplier an `add_monster_properties*` change adds (1 when none), or None
    when it sets properties the estimate can't handle."""
    groups = [change.get("properties")] + list((change.get("by_tag") or {}).values()) + list((change.get("by_id") or {}).values())
    mult = 1.0
    for props in (g for g in groups if g is not None):
        if not isinstance(props, dict):
            return None
        for key, value in props.items():
            if key == "health_multiplier" and isinstance(value, (int, float)):
                if change.get("affects_bosses", True) is not False:
                    mult = max(mult, float(value))
            elif key == "monster_effects":
                if not all(isinstance(e, str) and e.split(",", 1)[0].strip() in _HARMLESS_MONSTER_EFFECTS for e in value or ()):
                    return None
            elif key not in _HARMLESS_PROPERTIES:
                return None
    return mult


def _growth(curve: HealthCurve, area: int) -> float:
    rate = curve.growth[0][1] if curve.growth else 1.0
    for start, value in curve.growth:
        if area >= start:
            rate = value
    return rate


def _loadout_in_play(account: Account, champion_id: int) -> Loadout:
    owned = account.champions.get(champion_id)
    if owned is None:
        return Loadout()
    return Loadout(tuple(sorted(owned.specialization_choices)), tuple(sorted(owned.equipped_feats)))


def formation_in_play(catalogue: Catalogue, account: Account, instance: GameInstance) -> tuple[Seat, ...]:
    return tuple(
        Seat(champion_id, slot, _loadout_in_play(account, champion_id))
        for slot, champion_id in enumerate(instance.formation)
        if champion_id > 0 and catalogue.find(champion_id)
    )


def _sci(x: float) -> str:
    return f"{x:.2e}".replace("e+", "e")
