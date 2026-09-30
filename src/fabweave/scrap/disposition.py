from __future__ import annotations

from dataclasses import dataclass, replace

from fabweave.core.decision import Decision, allow, deny
from fabweave.core.events import Event, EventLog
from fabweave.core.ids import deterministic_id
from fabweave.shipment.commit import CommitLedger

REQUESTED = "requested"
REVIEWED = "reviewed"
APPROVED = "approved"
EXECUTED = "executed"
REVOKED = "revoked"
REVERSED = "reversed"

_REQUESTED = "scrap_requested"
_REVIEWED = "scrap_reviewed"
_APPROVED = "scrap_approved"
_EXECUTED = "scrap_executed"
_REVOKED = "scrap_revoked"
_REVERSED = "scrap_reversed"

_PHASES_BEFORE_EXECUTION = frozenset({REQUESTED, REVIEWED, APPROVED})


@dataclass(frozen=True, slots=True)
class ScrapPolicy:
    """Approval policy in force at event time (revision kept on every event)."""

    revision: int
    max_units: int
    min_units: int | None = None
    allow_reversal: bool = True

    def quantity_reasons(self, units: object, room: int) -> list[str]:
        if not isinstance(units, int) or units <= 0:
            return ["scrap_quantity_invalid"]
        reasons: list[str] = []
        if self.min_units is not None and units < self.min_units:
            reasons.append("scrap_quantity_below_minimum")
        if units > self.max_units:
            reasons.append("scrap_quantity_above_limit")
        if units > room:
            reasons.append("scrap_exceeds_uncommitted_inventory")
        return reasons


@dataclass(frozen=True, slots=True)
class ScrapState:
    request_id: str
    version: int
    phase: str
    requested_units: int
    requested_by: str
    requested_at: int
    reviewed_units: int | None = None
    reviewed_by: str | None = None
    reviewed_at: int | None = None
    approved_units: int | None = None
    approver: str | None = None
    approved_at: int | None = None
    executed_units: int | None = None
    executed_by: str | None = None
    executed_at: int | None = None
    reversed_units: int = 0
    reversed_by: str | None = None
    revoked_by: str | None = None
    revoked_at: int | None = None
    last_reason: str | None = None


def scrap_state(events: tuple[Event, ...]) -> ScrapState | None:
    """Fold the append-only scrap stream into its current state."""
    if not events:
        return None
    first = events[0]
    state = ScrapState(
        request_id=_request_id_of(first.stream),
        version=first.sequence,
        phase=REQUESTED,
        requested_units=first.data["units"],
        requested_by=first.data["requester"],
        requested_at=first.occurred_at,
        last_reason=first.data["reason"],
    )
    for event in events[1:]:
        data = event.data
        if event.kind == _REVIEWED:
            state = replace(
                state,
                version=event.sequence,
                phase=REVIEWED,
                reviewed_units=data["units"],
                reviewed_by=data["reviewer"],
                reviewed_at=event.occurred_at,
                last_reason=data["reason"],
            )
        elif event.kind == _APPROVED:
            state = replace(
                state,
                version=event.sequence,
                phase=APPROVED,
                approved_units=data["units"],
                approver=data["approver"],
                approved_at=event.occurred_at,
                last_reason=data["reason"],
            )
        elif event.kind == _EXECUTED:
            state = replace(
                state,
                version=event.sequence,
                phase=EXECUTED,
                executed_units=data["units"],
                executed_by=data["executed_by"],
                executed_at=event.occurred_at,
                last_reason=data["reason"],
            )
        elif event.kind == _REVOKED:
            state = replace(
                state,
                version=event.sequence,
                phase=REVOKED,
                revoked_by=data["authority"],
                revoked_at=event.occurred_at,
                last_reason=data["reason"],
            )
        else:  # _REVERSED — partial reversals stay EXECUTED, the last one flips to REVERSED
            reversed_units = state.reversed_units + data["units"]
            phase = REVERSED if reversed_units == state.executed_units else EXECUTED
            state = replace(
                state,
                version=event.sequence,
                phase=phase,
                reversed_units=reversed_units,
                reversed_by=data["authority"],
                last_reason=data["reason"],
            )
    return state


def _request_id_of(stream: str) -> str:
    return stream.split(":", 1)[1]


def _evidence(state: ScrapState | None, policy: ScrapPolicy, **extra: object) -> dict[str, object]:
    evidence: dict[str, object] = {"policy_revision": policy.revision}
    if state is not None:
        evidence["scrap_version"] = state.version
    evidence.update(extra)
    return evidence


def request_decision(policy: ScrapPolicy, units: int, requester: str, reason: str, room: int) -> Decision:
    reasons = [*policy.quantity_reasons(units, room)]
    if not requester:
        reasons.append("requester_missing")
    if not reason:
        reasons.append("reason_code_missing")
    evidence = _evidence(None, policy, requested_units=units, available_room=room)
    return deny(*reasons, **evidence) if reasons else allow(**evidence)


def review_decision(
    state: ScrapState | None,
    expected_version: int,
    units: int,
    reviewer: str,
    reason: str,
    at: int,
    policy: ScrapPolicy,
    room: int,
) -> Decision:
    evidence = _evidence(state, policy, reviewed_units=units, available_room=room)
    if state is None:
        return deny("scrap_request_not_found", **evidence)
    if expected_version != state.version:
        return deny("stale_scrap_version", **evidence, current_version=state.version)
    if state.phase != REQUESTED:
        return deny("scrap_phase_transition_denied", **evidence, phase=state.phase)
    reasons = [*policy.quantity_reasons(units, room)]
    if not reviewer:
        reasons.append("reviewer_missing")
    elif reviewer == state.requested_by:
        reasons.append("segregation_of_duties:reviewer_is_requester")
    if not reason:
        reasons.append("reason_code_missing")
    if at < state.requested_at:
        reasons.append("scrap_event_backdated")
    return deny(*reasons, **evidence) if reasons else allow(**evidence)


def approve_decision(
    state: ScrapState | None,
    expected_version: int,
    units: int,
    approver: str,
    reason: str,
    at: int,
    policy: ScrapPolicy,
    room: int,
) -> Decision:
    evidence = _evidence(state, policy, approved_units=units, available_room=room)
    if state is None:
        return deny("scrap_request_not_found", **evidence)
    if expected_version != state.version:
        return deny("stale_scrap_version", **evidence, current_version=state.version)
    if state.phase != REVIEWED:
        return deny("scrap_phase_transition_denied", **evidence, phase=state.phase)
    reasons: list[str] = []
    if not isinstance(units, int) or units <= 0:
        reasons.append("scrap_quantity_invalid")
    elif units > (state.reviewed_units or 0):
        reasons.append("scrap_quantity_above_reviewed")
    elif units > room:
        reasons.append("scrap_exceeds_uncommitted_inventory")
    if not approver:
        reasons.append("approver_missing")
    elif approver == state.requested_by:
        reasons.append("segregation_of_duties:approver_is_requester")
    elif approver == state.reviewed_by:
        reasons.append("segregation_of_duties:approver_is_reviewer")
    if not reason:
        reasons.append("reason_code_missing")
    if state.reviewed_at is not None and at < state.reviewed_at:
        reasons.append("scrap_event_backdated")
    return deny(*reasons, **evidence) if reasons else allow(**evidence)


def execute_decision(
    state: ScrapState | None,
    expected_version: int,
    executed_by: str,
    reason: str,
    at: int,
    policy: ScrapPolicy,
) -> Decision:
    evidence = _evidence(state, policy, approved_units=state.approved_units if state else None)
    if state is None:
        return deny("scrap_request_not_found", **evidence)
    if expected_version != state.version:
        return deny("stale_scrap_version", **evidence, current_version=state.version)
    if state.phase != APPROVED:
        return deny("scrap_phase_transition_denied", **evidence, phase=state.phase)
    reasons: list[str] = []
    if not executed_by:
        reasons.append("executor_missing")
    if not reason:
        reasons.append("reason_code_missing")
    if state.approved_at is not None and at < state.approved_at:
        reasons.append("scrap_event_backdated")
    return deny(*reasons, **evidence) if reasons else allow(**evidence)


def revoke_decision(
    state: ScrapState | None,
    expected_version: int,
    reason: str,
    authority: str,
    at: int,
    policy: ScrapPolicy,
    units: int | None = None,
) -> Decision:
    evidence = _evidence(state, policy, reversal=(state is not None and state.phase == EXECUTED), reversal_units=units)
    if state is None:
        return deny("scrap_request_not_found", **evidence)
    if expected_version != state.version:
        return deny("stale_scrap_version", **evidence, current_version=state.version)
    reversal = state.phase == EXECUTED
    reasons: list[str] = []
    if reversal:
        remaining = (state.executed_units or 0) - state.reversed_units
        if not policy.allow_reversal:
            reasons.append("scrap_reversal_disabled")
        if not isinstance(units, int) or units <= 0 or units > remaining:
            reasons.append("scrap_reversal_quantity_invalid")
        if not authority:
            reasons.append("reversal_authority_missing")
        reference_at = state.executed_at
    elif state.phase in _PHASES_BEFORE_EXECUTION:
        reference_at = _last_activity_at(state)
    else:  # revoked / fully reversed
        reasons.append("scrap_phase_transition_denied")
        reference_at = None
    if not reason:
        reasons.append("reason_code_missing")
    if reference_at is not None and at < reference_at:
        reasons.append("scrap_event_backdated")
    return deny(*reasons, **evidence) if reasons else allow(**evidence)


def _last_activity_at(state: ScrapState) -> int | None:
    return next((instant for instant in (state.approved_at, state.reviewed_at, state.requested_at) if instant is not None), None)


class ScrapOrders:
    """Event-sourced scrap requests coordinated with committed inventory."""

    def __init__(self, log: EventLog, ledger: CommitLedger, policy: ScrapPolicy) -> None:
        self._log = log
        self._ledger = ledger
        self._policy = policy

    def state(self, request_id: str) -> ScrapState | None:
        return scrap_state(self._log.read(self._stream(request_id)))

    def events(self, request_id: str) -> tuple[Event, ...]:
        return self._log.read(self._stream(request_id))

    def request(self, request_id: str, units: int, requester: str, reason_code: str, at: int) -> Decision:
        stream = self._stream(request_id)
        data = {"units": units, "requester": requester, "reason": reason_code, "policy_revision": self._policy.revision}
        events = self._log.read(stream)
        if events:
            if events[0].data != data or events[0].occurred_at != at:
                raise ValueError("scrap request id reused with different content")
            return allow(scrap_version=1, replay=True)
        decision = request_decision(self._policy, units, requester, reason_code, self._ledger.scrap_room_for(request_id))
        if not decision.allowed:
            return decision
        self._append(stream, 0, _REQUESTED, at, data)
        self._ledger.reserve_scrap(request_id, units)
        return allow(scrap_version=1, requested_units=units)

    def review(self, request_id: str, expected_version: int, units: int, reviewer: str, reason_code: str, at: int) -> Decision:
        data = {"units": units, "reviewer": reviewer, "reason": reason_code, "policy_revision": self._policy.revision}
        replayed = self._matching_event(request_id, _REVIEWED, at, data)
        if replayed is not None:
            return allow(scrap_version=replayed.sequence, replay=True)
        state = self.state(request_id)
        decision = review_decision(state, expected_version, units, reviewer, reason_code, at, self._policy, self._ledger.scrap_room_for(request_id))
        if not decision.allowed:
            return decision
        self._append(self._stream(request_id), expected_version, _REVIEWED, at, data)
        self._ledger.adjust_scrap_reservation(request_id, units)
        return allow(scrap_version=expected_version + 1, reviewed_units=units)

    def approve(self, request_id: str, expected_version: int, units: int, approver: str, reason_code: str, at: int) -> Decision:
        data = {"units": units, "approver": approver, "reason": reason_code, "policy_revision": self._policy.revision}
        replayed = self._matching_event(request_id, _APPROVED, at, data)
        if replayed is not None:
            return allow(scrap_version=replayed.sequence, replay=True)
        state = self.state(request_id)
        decision = approve_decision(state, expected_version, units, approver, reason_code, at, self._policy, self._ledger.scrap_room_for(request_id))
        if not decision.allowed:
            return decision
        self._append(self._stream(request_id), expected_version, _APPROVED, at, data)
        self._ledger.adjust_scrap_reservation(request_id, units)
        return allow(scrap_version=expected_version + 1, approved_units=units)

    def execute(self, request_id: str, expected_version: int, executed_by: str, reason_code: str, at: int) -> Decision:
        state = self.state(request_id)
        data = {
            "units": state.approved_units if state else None,
            "executed_by": executed_by,
            "reason": reason_code,
            "policy_revision": self._policy.revision,
        }
        replayed = self._matching_event(request_id, _EXECUTED, at, data)
        if replayed is not None:
            return allow(scrap_version=replayed.sequence, replay=True)
        decision = execute_decision(state, expected_version, executed_by, reason_code, at, self._policy)
        if not decision.allowed:
            return decision
        self._append(self._stream(request_id), expected_version, _EXECUTED, at, data)
        removed = self._ledger.confirm_scrap(request_id)
        return allow(scrap_version=expected_version + 1, scrapped_units=removed)

    def revoke(
        self,
        request_id: str,
        expected_version: int,
        reason_code: str,
        authority: str,
        at: int,
        units: int | None = None,
    ) -> Decision:
        state = self.state(request_id)
        is_reversal = state is not None and state.phase == EXECUTED
        kind = _REVERSED if is_reversal else _REVOKED
        data = (
            {"units": units, "authority": authority, "reason": reason_code, "policy_revision": self._policy.revision}
            if kind == _REVERSED
            else {"authority": authority, "reason": reason_code}
        )
        replayed = self._matching_event(request_id, kind, at, data)
        if replayed is not None:
            return allow(scrap_version=replayed.sequence, replay=True)
        decision = revoke_decision(state, expected_version, reason_code, authority, at, self._policy, units)
        if not decision.allowed:
            return decision
        self._append(
            self._stream(request_id),
            expected_version,
            kind,
            at,
            data,
            cumulative_reversed=state.reversed_units if is_reversal else None,
        )
        if kind == _REVERSED:
            # revoke_decision already bounded units to the remaining executed quantity
            restored = self._ledger.reverse_scrap(request_id, units if isinstance(units, int) else 0)
            return allow(scrap_version=expected_version + 1, reversal=True, reversed_units=restored)
        released = self._ledger.release_scrap(request_id)
        return allow(scrap_version=expected_version + 1, reversal=False, released_units=released)

    def _matching_event(self, request_id: str, kind: str, at: int, data: dict[str, object]) -> Event | None:
        """An identical command already happened: safe at-most-once redrive."""
        for event in self._log.read(self._stream(request_id)):
            if event.kind == kind and event.data == data and event.occurred_at == at:
                return event
        return None

    def _append(
        self,
        stream: str,
        expected_sequence: int,
        kind: str,
        at: int,
        data: dict[str, object],
        cumulative_reversed: int | None = None,
    ) -> Event:
        discriminator = (kind, cumulative_reversed) if cumulative_reversed is not None else kind
        event_id = deterministic_id("SCRAP", _request_id_of(stream), discriminator)
        return self._log.append(stream, expected_sequence, event_id, at, kind, data)

    @staticmethod
    def _stream(request_id: str) -> str:
        if not request_id:
            raise ValueError("scrap request id missing")
        return f"scrap:{request_id}"
