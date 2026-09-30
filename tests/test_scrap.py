from __future__ import annotations

import pytest

from fabweave.core.events import EventLog
from fabweave.core.quantities import require_conservation
from fabweave.scrap import (
    APPROVED,
    EXECUTED,
    REQUESTED,
    REVERSED,
    REVIEWED,
    REVOKED,
    ScrapOrders,
    ScrapPolicy,
    approve_decision,
    execute_decision,
    request_decision,
    review_decision,
    revoke_decision,
)
from fabweave.shipment.commit import CommitLedger


POLICY = ScrapPolicy(revision=1, max_units=30, min_units=1)


def _service(good: int = 100, safety: int = 0, policy: ScrapPolicy = POLICY):
    ledger = CommitLedger(good, safety_units=safety)
    return ScrapOrders(EventLog(), ledger, policy), ledger


def _drive(svc: ScrapOrders, request_id: str, units: int, at: int = 100) -> bool:
    assert svc.request(request_id, units, "OPERATOR", "DAMAGE", at).allowed
    assert svc.review(request_id, 1, units, "ENGINEER", "CONFIRMED", at + 10).allowed
    assert svc.approve(request_id, 2, units, "MANAGER", "APPROVED", at + 20).allowed
    assert svc.execute(request_id, 3, "EXECUTOR", "SCRAPPED", at + 30).allowed
    return True


def test_scrap_happy_path_holds_then_removes_units_once():
    svc, ledger = _service(100)
    ledger.allocate("ORD-1", "ORDER-1", 40)
    assert ledger.available == 60
    assert svc.request("SCRAP-1", 10, "OP", "DAMAGE", 100).allowed
    assert ledger.available == 50 and ledger.pending_scrap == 10 and ledger.scrapped == 0
    assert svc.review("SCRAP-1", 1, 8, "ENG", "OK", 110).allowed
    assert ledger.pending_scrap == 8
    assert svc.approve("SCRAP-1", 2, 8, "MGR", "OK", 120).allowed
    assert svc.execute("SCRAP-1", 3, "EXE", "GO", 130).allowed
    # the hold moves into the scrapped bucket; order allocation is untouched (40 + 52 + 8 = 100)
    assert ledger.available == 52 and ledger.scrapped == 8 and ledger.pending_scrap == 0
    state = svc.state("SCRAP-1")
    assert state.phase == EXECUTED and state.version == 4 and state.executed_units == 8
    # execution conserves inventory: the held units move into the scrapped bucket
    require_conservation(
        {"good": ledger.available, "allocated": 40, "scrapped": ledger.scrapped}, {"total": 100}
    )
    # every decision is an audited versioned event with reason and actor
    kinds = [event.kind for event in svc.events("SCRAP-1")]
    assert kinds == ["scrap_requested", "scrap_reviewed", "scrap_approved", "scrap_executed"]
    assert [event.data["reason"] for event in svc.events("SCRAP-1")] == ["DAMAGE", "OK", "OK", "GO"]


def test_stages_cannot_run_out_of_order():
    svc, ledger = _service(50)
    assert svc.execute("SCRAP-1", 1, "EXE", "GO", 130).reasons == ("scrap_request_not_found",)
    svc.request("SCRAP-1", 5, "OP", "R", 100)
    assert svc.execute("SCRAP-1", 1, "EXE", "GO", 130).reasons == ("scrap_phase_transition_denied",)
    assert svc.approve("SCRAP-1", 1, 5, "MGR", "R", 120).reasons == ("scrap_phase_transition_denied",)
    svc.review("SCRAP-1", 1, 5, "ENG", "R", 110)
    assert svc.execute("SCRAP-1", 2, "EXE", "R", 130).reasons == ("scrap_phase_transition_denied",)
    # no stage advanced the ledger on a rejected transition
    assert ledger.pending_scrap == 5 and ledger.scrapped == 0


def test_duplicate_commands_are_idempotent_and_never_deduct_twice():
    svc, ledger = _service(100)
    _drive(svc, "SCRAP-1", 10)
    assert ledger.scrapped == 10
    for _ in range(3):
        decision = svc.execute("SCRAP-1", 3, "EXECUTOR", "SCRAPPED", 130)
        assert decision.allowed and decision.evidence["replay"] is True
    assert ledger.scrapped == 10 and len(svc.events("SCRAP-1")) == 4
    # identical redrives of every earlier stage are replays too
    assert svc.request("SCRAP-1", 10, "OPERATOR", "DAMAGE", 100).evidence["replay"] is True
    assert svc.review("SCRAP-1", 1, 10, "ENGINEER", "CONFIRMED", 110).evidence["replay"] is True
    assert svc.approve("SCRAP-1", 2, 10, "MANAGER", "APPROVED", 120).evidence["replay"] is True


def test_stale_expected_version_blocks_the_command():
    svc, _ = _service(50)
    svc.request("SCRAP-1", 5, "OP", "R", 100)
    decision = svc.review("SCRAP-1", 9, 5, "ENG", "R", 110)
    assert decision.reasons == ("stale_scrap_version",)
    assert decision.evidence["current_version"] == 1
    assert svc.state("SCRAP-1").phase == REQUESTED


def test_quantity_cannot_touch_committed_order_units():
    svc, ledger = _service(100, safety=5)
    ledger.allocate("ORD-1", "ORDER-1", 80)
    assert ledger.available == 15
    denied = svc.request("SCRAP-1", 16, "OP", "R", 100)
    assert denied.reasons == ("scrap_exceeds_uncommitted_inventory",)
    # a valid hold later cannot be inflated through review or approval into committed stock
    assert svc.request("SCRAP-1", 15, "OP", "R", 100).allowed
    assert svc.review("SCRAP-1", 1, 15, "ENG", "R", 110).allowed
    assert svc.approve("SCRAP-1", 2, 15, "MGR", "R", 120).allowed
    # the order allocation itself is intact
    assert sum(item.units for item in ledger._allocations.values()) == 80
    # new orders can never consume scrap-held units
    with pytest.raises(ValueError):
        ledger.allocate("ORD-2", "ORDER-2", 1)


def test_approval_cannot_exceed_engineer_reviewed_quantity():
    svc, ledger = _service(100)
    svc.request("SCRAP-1", 10, "OP", "R", 100)
    svc.review("SCRAP-1", 1, 6, "ENG", "R", 110)
    decision = svc.approve("SCRAP-1", 2, 7, "MGR", "R", 120)
    assert decision.reasons == ("scrap_quantity_above_reviewed",)
    assert ledger.pending_scrap == 6


def test_segregation_of_duties_is_enforced():
    svc, _ = _service(50)
    svc.request("SCRAP-1", 5, "OP", "R", 100)
    assert svc.review("SCRAP-1", 1, 5, "OP", "R", 110).reasons == ("segregation_of_duties:reviewer_is_requester",)
    svc.review("SCRAP-1", 1, 5, "ENG", "R", 111)
    assert svc.approve("SCRAP-1", 2, 5, "OP", "R", 120).reasons == ("segregation_of_duties:approver_is_requester",)
    assert svc.approve("SCRAP-1", 2, 5, "ENG", "R", 120).reasons == ("segregation_of_duties:approver_is_reviewer",)


def test_cancellation_before_execution_releases_the_reservation():
    svc, ledger = _service(100)
    svc.request("SCRAP-1", 10, "OP", "R", 100)
    svc.review("SCRAP-1", 1, 10, "ENG", "R", 110)
    assert ledger.available == 90
    decision = svc.revoke("SCRAP-1", 2, "WITHDRAWN", "PLANNER", 115)
    assert decision.allowed and decision.evidence["released_units"] == 10
    assert ledger.available == 100 and ledger.pending_scrap == 0
    assert svc.state("SCRAP-1").phase == REVOKED
    # a revoked request cannot be executed or revoked again
    assert svc.execute("SCRAP-1", 3, "EXE", "R", 130).reasons == ("scrap_phase_transition_denied",)
    assert svc.revoke("SCRAP-1", 3, "AGAIN", "PLANNER", 116).reasons == ("scrap_phase_transition_denied",)


def test_controlled_reversal_restores_executed_units_and_is_audited():
    svc, ledger = _service(100)
    _drive(svc, "SCRAP-1", 10)
    assert ledger.scrapped == 10 and ledger.available == 90
    require_conservation({"good": ledger.available, "scrapped": ledger.scrapped}, {"total": 100})
    decision = svc.revoke("SCRAP-1", 4, "FALSE_SCRAP", "QA-LEAD", 200, units=4)
    assert decision.allowed and decision.evidence["reversal"] is True
    assert ledger.scrapped == 6 and ledger.available == 94
    state = svc.state("SCRAP-1")
    assert state.phase == EXECUTED and state.reversed_units == 4
    require_conservation({"good": ledger.available, "scrapped": ledger.scrapped}, {"total": 100})
    decision = svc.revoke("SCRAP-1", 5, "FALSE_SCRAP", "QA-LEAD", 210, units=6)
    assert decision.allowed
    assert ledger.scrapped == 0 and ledger.available == 100
    final = svc.state("SCRAP-1")
    assert final.phase == REVERSED and final.reversed_units == 10 and final.version == 6


def test_duplicate_reversal_is_idempotent_but_different_redrive_is_rejected():
    svc, ledger = _service(100)
    _drive(svc, "SCRAP-1", 10)
    svc.revoke("SCRAP-1", 4, "FALSE_SCRAP", "QA-LEAD", 200, units=4)
    assert ledger.scrapped == 6
    again = svc.revoke("SCRAP-1", 4, "FALSE_SCRAP", "QA-LEAD", 200, units=4)
    assert again.evidence["replay"] is True
    assert ledger.scrapped == 6
    # same call shape but a different quantity is a new command on a stale version -> denied, no restore
    rejected = svc.revoke("SCRAP-1", 4, "FALSE_SCRAP", "QA-LEAD", 200, units=3)
    assert rejected.reasons == ("stale_scrap_version",)
    assert ledger.scrapped == 6


def test_reversal_quantity_is_bounded_by_remaining_executed_units():
    svc, ledger = _service(100)
    _drive(svc, "SCRAP-1", 10)
    svc.revoke("SCRAP-1", 4, "FALSE_SCRAP", "QA-LEAD", 200, units=4)
    too_much = svc.revoke("SCRAP-1", 5, "X", "QA-LEAD", 210, units=7)
    assert "scrap_reversal_quantity_invalid" in too_much.reasons
    missing_units = svc.revoke("SCRAP-1", 5, "X", "QA-LEAD", 210)
    assert "scrap_reversal_quantity_invalid" in missing_units.reasons
    assert ledger.scrapped == 6


def test_policy_can_disable_reversal_without_blocking_execution():
    svc, ledger = _service(50, policy=ScrapPolicy(2, 30, allow_reversal=False))
    _drive(svc, "SCRAP-1", 5)
    decision = svc.revoke("SCRAP-1", 4, "FALSE_SCRAP", "QA-LEAD", 200, units=5)
    assert "scrap_reversal_disabled" in decision.reasons
    assert ledger.scrapped == 5 and ledger.available == 45
    require_conservation({"good": ledger.available, "scrapped": ledger.scrapped}, {"total": 50})


def test_every_command_requires_a_reason_code():
    svc, ledger = _service(50)
    assert "reason_code_missing" in svc.request("SCRAP-1", 5, "OP", "", 100).reasons
    assert svc.request("SCRAP-1", 5, "OP", "R", 100).allowed
    assert "reason_code_missing" in svc.review("SCRAP-1", 1, 5, "ENG", "", 110).reasons
    svc.review("SCRAP-1", 1, 5, "ENG", "R", 111)
    assert "reason_code_missing" in svc.approve("SCRAP-1", 2, 5, "MGR", "", 120).reasons
    svc.approve("SCRAP-1", 2, 5, "MGR", "R", 121)
    assert "reason_code_missing" in svc.execute("SCRAP-1", 3, "EXE", "", 130).reasons
    assert ledger.scrapped == 0


def test_policy_limits_and_versions_are_recorded_on_events():
    svc, _ = _service(50, policy=ScrapPolicy(7, 30, min_units=2))
    assert svc.request("SCRAP-1", 1, "OP", "R", 100).reasons == ("scrap_quantity_below_minimum",)
    assert svc.request("SCRAP-1", 31, "OP", "R", 100).reasons == ("scrap_quantity_above_limit",)
    svc.request("SCRAP-1", 5, "OP", "R", 100)
    assert all(event.data["policy_revision"] == 7 for event in svc.events("SCRAP-1"))


def test_backdated_commands_are_rejected():
    svc, _ = _service(50)
    svc.request("SCRAP-1", 5, "OP", "R", 100)
    assert "scrap_event_backdated" in svc.review("SCRAP-1", 1, 5, "ENG", "R", 90).reasons


def test_inventory_is_conserved_through_execution_and_full_reversal():
    svc, ledger = _service(100)
    ledger.allocate("ORD-1", "ORDER-1", 20)
    _drive(svc, "SCRAP-1", 10)
    assert ledger.available == 70
    require_conservation(
        {"good": ledger.available, "allocated": 20, "scrapped": ledger.scrapped}, {"total": 100}
    )
    svc.revoke("SCRAP-1", 4, "FALSE_SCRAP", "QA-LEAD", 200, units=10)
    assert ledger.available == 80
    require_conservation(
        {"good": ledger.available, "allocated": 20, "scrapped": ledger.scrapped}, {"total": 100}
    )


def test_pure_decision_functions_report_all_reasons_together():
    policy = ScrapPolicy(1, 5)
    assert request_decision(policy, 6, "", "", 0).reasons == (
        "scrap_quantity_above_limit",
        "scrap_exceeds_uncommitted_inventory",
        "requester_missing",
        "reason_code_missing",
    )
    assert execute_decision(None, 1, "EXE", "R", 130, policy).reasons == ("scrap_request_not_found",)
    assert revoke_decision(None, 1, "R", "LEAD", 130, policy, units=1).reasons == ("scrap_request_not_found",)


def test_post_execution_reversal_requires_an_authority():
    svc, ledger = _service(50)
    _drive(svc, "SCRAP-1", 5)
    decision = svc.revoke("SCRAP-1", 4, "FALSE_SCRAP", "", 200, units=5)
    assert "reversal_authority_missing" in decision.reasons
    assert ledger.scrapped == 5 and ledger.available == 45


def test_state_is_replayable_from_the_audit_stream():
    log, ledger = EventLog(), CommitLedger(100)
    svc = ScrapOrders(log, ledger, POLICY)
    _drive(svc, "SCRAP-1", 10)
    rebuilt = ScrapOrders(log, ledger, POLICY)
    state = rebuilt.state("SCRAP-1")
    assert state.phase == EXECUTED and state.version == 4 and state.executed_units == 10
    assert state.requested_by == "OPERATOR" and state.approver == "MANAGER"
    # the rebuilt service still treats an execute redrive as a replay, not a new deduction
    assert rebuilt.execute("SCRAP-1", 3, "EXECUTOR", "SCRAPPED", 130).evidence["replay"] is True
    assert ledger.scrapped == 10


def test_cancellation_works_from_any_pre_execution_phase():
    svc, ledger = _service(50)
    svc.request("SCRAP-1", 5, "OP", "R", 100)
    svc.review("SCRAP-1", 1, 5, "ENG", "R", 110)
    svc.approve("SCRAP-1", 2, 5, "MGR", "R", 120)
    decision = svc.revoke("SCRAP-1", 3, "WITHDRAWN", "PLANNER", 125)
    assert decision.allowed and decision.evidence["released_units"] == 5
    assert ledger.available == 50 and ledger.scrapped == 0
    assert svc.state("SCRAP-1").phase == REVOKED
