from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fabweave.core.events import EventLog
from fabweave.core.ids import validate_id


REQUESTED = "requested"
ENGINEERING_REVIEWED = "engineering_reviewed"
APPROVED = "approved"
EXECUTED = "executed"
REVERSED = "reversed"

STATE_ORDER: tuple[str, ...] = (REQUESTED, ENGINEERING_REVIEWED, APPROVED, EXECUTED, REVERSED)

_PREDECESSORS: dict[str, tuple[str, ...]] = {
    ENGINEERING_REVIEWED: (REQUESTED,),
    APPROVED: (ENGINEERING_REVIEWED,),
    EXECUTED: (APPROVED,),
    REVERSED: (EXECUTED,),
}

_EVENT_KIND: dict[str, str] = {
    REQUESTED: "scrap_requested",
    ENGINEERING_REVIEWED: "scrap_reviewed",
    APPROVED: "scrap_approved",
    EXECUTED: "scrap_executed",
    REVERSED: "scrap_reversed",
}


@dataclass(frozen=True, slots=True)
class ScrapDecisionRecord:
    """One decision taken against a scrap case, with its version and reason."""

    version: int
    state: str
    allowed: bool
    reasons: tuple[str, ...]
    reason: str
    actor: str
    decided_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "state": self.state,
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "reason": self.reason,
            "actor": self.actor,
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True, slots=True)
class ScrapCase:
    scrap_id: str
    version: int
    state: str
    units: int
    reason: str
    defect_code: str | None
    created_at: int
    decisions: tuple[ScrapDecisionRecord, ...] = ()

    @property
    def last_decision(self) -> ScrapDecisionRecord | None:
        return self.decisions[-1] if self.decisions else None


class ScrapLedger:
    """Stateful scrap/write-off aggregate.

    A case moves strictly through request -> engineering review -> approval ->
    execution -> controlled reversal.  Every transition is version-checked,
    recorded with its reason and appended to an audit log.  Execution deducts
    quantity exactly once; reversal restores it exactly once.  Out-of-order or
    duplicate operations never mutate quantity, and units already committed to
    orders can never be scrapped.
    """

    def __init__(self, good_units: int, event_log: EventLog | None = None) -> None:
        if not isinstance(good_units, int) or good_units < 0:
            raise ValueError("good_units must be a non-negative integer")
        self._good = good_units
        self._cases: dict[str, ScrapCase] = {}
        self._log = event_log if event_log is not None else EventLog()
        self._pending = 0
        self._scrapped = 0

    @property
    def good_units(self) -> int:
        return self._good

    @property
    def pending_scrap_units(self) -> int:
        return self._pending

    @property
    def scrapped_units(self) -> int:
        return self._scrapped

    def available_for_scrap(self, committed_units: int) -> int:
        if not isinstance(committed_units, int) or committed_units < 0:
            raise ValueError("committed_units must be a non-negative integer")
        return self._good - committed_units - self._pending - self._scrapped

    def get(self, scrap_id: str) -> ScrapCase:
        try:
            return self._cases[scrap_id]
        except KeyError:
            raise ValueError("unknown scrap case") from None

    def request(
        self,
        scrap_id: str,
        units: int,
        reason: str,
        defect_code: str | None,
        committed_units: int,
        at: int,
        actor: str,
        event_id: str,
    ) -> ScrapCase:
        validate_id(scrap_id)
        if not isinstance(units, int) or units <= 0:
            raise ValueError("scrap units must be a positive integer")
        if not reason:
            raise ValueError("scrap reason is required")
        existing = self._cases.get(scrap_id)
        if existing is not None:
            same = (
                existing.state == REQUESTED
                and existing.units == units
                and existing.reason == reason
                and existing.defect_code == defect_code
            )
            if not same:
                raise ValueError("scrap id reused with a different request")
            return existing
        if units > self.available_for_scrap(committed_units):
            raise ValueError("scrap units exceed uncommitted inventory")
        record = ScrapDecisionRecord(1, REQUESTED, True, (), reason, actor, at)
        case = ScrapCase(scrap_id, 1, REQUESTED, units, reason, defect_code, at, (record,))
        self._cases[scrap_id] = case
        self._pending += units
        self._append(scrap_id, event_id, at, record, units=units, defect_code=defect_code)
        return case

    def review(
        self,
        scrap_id: str,
        expected_version: int,
        event_id: str,
        actor: str,
        at: int,
        note: str = "",
    ) -> ScrapCase:
        return self._transition(scrap_id, ENGINEERING_REVIEWED, expected_version, event_id, actor, note, at)

    def approve(
        self,
        scrap_id: str,
        expected_version: int,
        event_id: str,
        actor: str,
        at: int,
        note: str = "",
    ) -> ScrapCase:
        return self._transition(scrap_id, APPROVED, expected_version, event_id, actor, note, at)

    def execute(
        self,
        scrap_id: str,
        expected_version: int,
        committed_units: int,
        event_id: str,
        actor: str,
        at: int,
    ) -> ScrapCase:
        return self._transition(
            scrap_id,
            EXECUTED,
            expected_version,
            event_id,
            actor,
            "",
            at,
            committed_units=committed_units,
        )

    def reverse(
        self,
        scrap_id: str,
        expected_version: int,
        reason: str,
        event_id: str,
        actor: str,
        at: int,
    ) -> ScrapCase:
        if not reason:
            raise ValueError("a reversal reason is required")
        return self._transition(scrap_id, REVERSED, expected_version, event_id, actor, reason, at)

    def _transition(
        self,
        scrap_id: str,
        target: str,
        expected_version: int,
        event_id: str,
        actor: str,
        reason: str,
        at: int,
        committed_units: int | None = None,
    ) -> ScrapCase:
        case = self.get(scrap_id)
        if target not in _PREDECESSORS:
            raise ValueError("unknown scrap target state")

        # Idempotent replay: this exact transition was already recorded.
        if self._log.has_event(event_id):
            return case

        # Optimistic concurrency: the case has moved on since the caller read it.
        if expected_version != case.version:
            return self._deny(case, target, event_id, actor, reason, at, ("stale_scrap_version",), {"current_version": case.version})

        # Duplicate: the case already reached the target through another path.
        if case.state == target:
            raise ValueError("scrap transition already applied")

        # Strict ordering: the predecessor gate must have been passed first.
        if case.state not in _PREDECESSORS[target]:
            return self._deny(
                case,
                target,
                event_id,
                actor,
                reason,
                at,
                (f"invalid_state_transition:{case.state}->{target}",),
                {},
            )

        # Fail-closed: units promised to orders can never be deducted.
        if target == EXECUTED:
            if committed_units is None:
                raise ValueError("committed_units is required for execution")
            if not isinstance(committed_units, int) or committed_units < 0:
                raise ValueError("committed_units must be a non-negative integer")
            if self._scrapped + case.units > self._good - committed_units:
                return self._deny(
                    case,
                    target,
                    event_id,
                    actor,
                    reason,
                    at,
                    ("committed_units_protected",),
                    {"committed_units": committed_units},
                )

        return self._apply(case, target, event_id, actor, reason, at)

    def _apply(
        self,
        case: ScrapCase,
        target: str,
        event_id: str,
        actor: str,
        reason: str,
        at: int,
    ) -> ScrapCase:
        new_version = case.version + 1
        record = ScrapDecisionRecord(new_version, target, True, (), reason or case.reason, actor, at)
        if target == EXECUTED:
            self._pending -= case.units
            self._scrapped += case.units
        elif target == REVERSED:
            self._scrapped -= case.units
        updated = ScrapCase(
            case.scrap_id,
            new_version,
            target,
            case.units,
            case.reason,
            case.defect_code,
            case.created_at,
            case.decisions + (record,),
        )
        self._cases[case.scrap_id] = updated
        self._append(case.scrap_id, event_id, at, record)
        return updated

    def _deny(
        self,
        case: ScrapCase,
        target: str,
        event_id: str,
        actor: str,
        reason: str,
        at: int,
        reasons: tuple[str, ...],
        evidence: dict[str, Any],
    ) -> ScrapCase:
        record = ScrapDecisionRecord(case.version, target, False, reasons, reason, actor, at)
        updated = ScrapCase(
            case.scrap_id,
            case.version,
            case.state,
            case.units,
            case.reason,
            case.defect_code,
            case.created_at,
            case.decisions + (record,),
        )
        self._cases[case.scrap_id] = updated
        payload = record.as_dict()
        payload.update(evidence)
        self._log.append(f"scrap:{case.scrap_id}", len(self._log.read(f"scrap:{case.scrap_id}")), event_id, at, _EVENT_KIND[target], payload)
        return updated

    def _append(
        self,
        scrap_id: str,
        event_id: str,
        at: int,
        record: ScrapDecisionRecord,
        units: int | None = None,
        defect_code: str | None = None,
    ) -> None:
        payload = record.as_dict()
        if units is not None:
            payload["units"] = units
        if defect_code is not None:
            payload["defect_code"] = defect_code
        stream = f"scrap:{scrap_id}"
        self._log.append(stream, len(self._log.read(stream)), event_id, at, _EVENT_KIND[record.state], payload)
