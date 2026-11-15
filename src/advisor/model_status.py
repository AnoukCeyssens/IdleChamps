"""Model Status and the live damage check (spec #22 component 9, #12).

Every champion has a Model Status:

- **Not Modelled**: no reconstruction of its unique effects (`champion_models.RECONSTRUCTIONS`); it
  is scored on its generic effects only. Validation never changes it.
- **Verified**: its reconstruction has description tests (kept passing by the test suite) and the
  latest live damage check on a formation holding it came within ±2% of the game's number.
- **Check Failed**: the latest live damage check on a formation holding it missed by more than 2%.
  Rounding and cap mismatches count as failures; the tolerance is never widened. A failure raises a
  model review flag, open until a later check passes for every champion it names.
- **Stale**: its reconstruction hasn't been checked against the current definitions: its
  `last_rework_date` or the definitions touching it (hero row, upgrades, feats, gear, attacks)
  changed since, or it was never checked.

**Live damage check** (`damage_checks`): for each running instance, the model predicts the Main
DPS's biggest hit for the formation in play (`Scorer.score(...).bud`) and compares it with the game's
`this_reset_highest_damage_dealt`. It only counts when every champion in that formation has a
reconstruction: the generic layer alone can't predict the game's absolute number (it is shown as
inconclusive). Each observation (instance, BUD, formation) is counted once.

`advance` applies one Refresh to the `Ledger` (the persisted state: per-champion records, flags,
observations already counted); the Store keeps it in `model_status.json.gz`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from typing import Any

from advisor.account import Account
from advisor.catalogue import Catalogue, Champion
from advisor.champion_models import RECONSTRUCTIONS, ChampionModels, ModelStatus
from advisor.push_verdict import formation_in_play

MODEL_STATUS_VERSION = 1
TOLERANCE_PCT = 2.0  # fixed: a mismatch from rounding or caps is a failure, never a reason to widen it
_EVIDENCE_KEPT = 10  # per champion, newest last


@dataclass(frozen=True)
class Evidence:
    text: str
    passed: bool
    at: int | None = None  # the snapshot's server time


@dataclass(frozen=True)
class ChampionRecord:
    status: ModelStatus
    fingerprint: str  # of the definitions touching the champion, when last looked at
    rework_date: str
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class ReviewFlag:
    """A model review flag: the model may be wrong for these champions."""

    text: str
    champion_ids: tuple[int, ...]
    at: int | None = None
    open: bool = True


@dataclass(frozen=True)
class Ledger:
    records: Mapping[int, ChampionRecord] = field(default_factory=dict)  # reconstructed champions only
    flags: tuple[ReviewFlag, ...] = ()
    observed: Mapping[int, str] = field(default_factory=dict)  # instance id -> observation already counted

    def statuses(self) -> dict[int, ModelStatus]:
        return {i: r.status for i, r in self.records.items()}

    def open_flags(self) -> tuple[ReviewFlag, ...]:
        return tuple(f for f in self.flags if f.open)


@dataclass(frozen=True)
class DamageCheck:
    instance_id: int
    adventure_name: str
    main_dps: Champion
    predicted: float
    actual: float
    error_pct: float  # (predicted / actual - 1) × 100, capped for display
    outcome: str  # "passed" | "failed" | "inconclusive"
    reason: str
    champion_ids: tuple[int, ...]
    formation: tuple[int, ...]

    @property
    def counted(self) -> bool:
        return self.outcome != "inconclusive"

    def summary(self) -> str:
        return (
            f"model {_sci(self.predicted)} vs game {_sci(self.actual)} ({_signed(self.error_pct)}, "
            f"tolerance ±{TOLERANCE_PCT:g}%)"
        )


def damage_checks(catalogue: Catalogue, account: Account, models: ChampionModels) -> tuple[DamageCheck, ...]:
    """The live damage check on every running instance that has a BUD."""
    checks = []
    for instance in account.instances:
        if instance.bud is None or instance.bud_champion_id is None or catalogue.find(instance.bud_champion_id) is None:
            continue
        seats = formation_in_play(catalogue, account, instance)
        main = next((s for s in seats if s.champion_id == instance.bud_champion_id), None)
        adventure = catalogue.adventure(instance.adventure_id)
        name = adventure.name if adventure else f"Adventure {instance.adventure_id}"
        ids = tuple(s.champion_id for s in seats)
        champion = catalogue.champion(instance.bud_champion_id)
        if main is None:
            checks.append(DamageCheck(
                instance.id, name, champion, math.nan, instance.bud, math.nan, "inconclusive",
                f"{champion.name} set the BUD but is no longer in the formation", ids, instance.formation,
            ))
            continue
        predicted = models.scorer(adventure.layout if adventure else (), "Push", main).score(seats).bud
        error = 100 * math.expm1(min(math.log(predicted) - math.log(instance.bud), 10.0))
        unmodelled = [i for i in ids if models.reconstruction(i) is None]
        if unmodelled:
            outcome = "inconclusive"
            reason = f"{len(unmodelled)} of {len(ids)} champions Not Modelled; the generic layer can't predict the game's number"
        elif abs(error) <= TOLERANCE_PCT:
            outcome, reason = "passed", f"within ±{TOLERANCE_PCT:g}%"
        else:
            outcome, reason = "failed", f"off by more than ±{TOLERANCE_PCT:g}%"
        checks.append(DamageCheck(instance.id, name, champion, predicted, instance.bud, error, outcome, reason, ids, instance.formation))
    return tuple(checks)


def advance(ledger: Ledger, catalogue: Catalogue, account: Account, checks: tuple[DamageCheck, ...]) -> Ledger:
    """The ledger after one Refresh: Stale transitions first, then the damage checks not yet counted."""
    at = account.server_time
    records = dict(ledger.records)
    for champion_id in RECONSTRUCTIONS:
        champion = catalogue.find(champion_id)
        if champion is None:
            continue
        fingerprint = champion_fingerprint(catalogue, champion_id)
        record = records.get(champion_id)
        if record is None:
            stale = Evidence("Not checked against the game yet: needs a live damage check.", False, at)
        elif record.rework_date != champion.last_rework_date:
            stale = Evidence(
                f"Reworked ({record.rework_date or 'no date'} → {champion.last_rework_date or 'no date'}): "
                "needs a new live damage check.", False, at,
            )
        elif record.fingerprint != fingerprint:
            stale = Evidence("Its definitions changed: needs a new live damage check.", False, at)
        else:
            continue
        evidence = (*(record.evidence if record else ()), stale)[-_EVIDENCE_KEPT:]
        records[champion_id] = ChampionRecord(ModelStatus.STALE, fingerprint, champion.last_rework_date, evidence)

    observed = dict(ledger.observed)
    flags = list(ledger.flags)
    for check in checks:
        key = json.dumps([repr(check.actual), list(check.formation)])
        if not check.counted or observed.get(check.instance_id) == key:
            continue
        observed[check.instance_id] = key
        passed = check.outcome == "passed"
        text = f"Live damage check on instance {check.instance_id} ({check.adventure_name}): {check.summary()}."
        for champion_id in dict.fromkeys(check.champion_ids):
            record = records.get(champion_id)
            if record is None:
                continue
            tested = bool(RECONSTRUCTIONS[champion_id].description_tests)
            status = (ModelStatus.VERIFIED if tested else record.status) if passed else ModelStatus.CHECK_FAILED
            evidence = (*record.evidence, Evidence(text, passed, at))[-_EVIDENCE_KEPT:]
            records[champion_id] = replace(record, status=status, evidence=evidence)
        if not passed:
            names = ", ".join(catalogue.champion(i).name for i in dict.fromkeys(check.champion_ids))
            flags.append(ReviewFlag(
                f"Live damage check failed on instance {check.instance_id} ({check.adventure_name}), Main DPS "
                f"{check.main_dps.name}: {check.summary()}. Champions marked Check Failed: {names}.",
                tuple(dict.fromkeys(check.champion_ids)), at,
            ))
    flags = [
        replace(f, open=False)
        if f.open and all(records.get(i) and records[i].status is ModelStatus.VERIFIED for i in f.champion_ids)
        else f
        for f in flags
    ]
    return Ledger(records, tuple(flags), observed)


def champion_fingerprint(catalogue: Catalogue, champion_id: int) -> str:
    """Hash of the definitions touching one champion: its hero row, upgrades, feats, gear and attacks."""
    champion = catalogue.champion(champion_id)
    parts = {
        "champion": champion,
        "upgrades": catalogue.upgrades_of(champion_id),
        "feats": catalogue.feats_of(champion_id),
        "specializations": catalogue.specializations.get(champion_id, ()),
        "gear": sorted((k, v) for k, v in catalogue.gear.items() if k[0] == champion_id),
        "attacks": [catalogue.attacks.get(a) for a in (champion.base_attack_id, champion.ultimate_attack_id) if a],
    }
    text = json.dumps(_plain(parts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


# --- persistence (Store: model_status.json.gz) ----------------------------------------------------


def to_json(ledger: Ledger) -> dict:
    return {
        "version": MODEL_STATUS_VERSION,
        "records": {
            str(i): {
                "status": r.status.value, "fingerprint": r.fingerprint, "rework_date": r.rework_date,
                "evidence": [asdict(e) for e in r.evidence],
            }
            for i, r in ledger.records.items()
        },
        "flags": [{**asdict(f), "champion_ids": list(f.champion_ids)} for f in ledger.flags],
        "observed": {str(k): v for k, v in ledger.observed.items()},
    }


def from_json(data: Mapping[str, Any] | None) -> Ledger:
    if not data or data.get("version") != MODEL_STATUS_VERSION:
        return Ledger()
    try:
        return Ledger(
            {
                int(i): ChampionRecord(
                    ModelStatus(r["status"]), r["fingerprint"], r["rework_date"],
                    tuple(Evidence(**e) for e in r.get("evidence") or ()),
                )
                for i, r in (data.get("records") or {}).items()
            },
            tuple(ReviewFlag(f["text"], tuple(f["champion_ids"]), f.get("at"), f.get("open", True)) for f in data.get("flags") or ()),
            {int(k): v for k, v in (data.get("observed") or {}).items()},
        )
    except (KeyError, TypeError, ValueError):
        return Ledger()


def _sci(x: float) -> str:
    return "?" if not math.isfinite(x) else f"{x:.3g}" if x < 1e6 else f"{x:.2e}".replace("e+", "e")


def _signed(pct: float) -> str:
    if not math.isfinite(pct):
        return "?"
    if pct >= 10_000:
        return "> +9999%"
    return f"{pct:+.1f}%"
