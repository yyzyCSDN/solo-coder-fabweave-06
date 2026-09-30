from __future__ import annotations

import pytest

from fabweave.core.events import EventLog
from fabweave.scrap.writeoff import (
    APPROVED,
    ENGINEERING_REVIEWED,
    EXECUTED,
    REQUESTED,
    REVERSED,
    ScrapLedger,
)
from fabweave.shipment.commit import CommitLedger
from fabweave.workflows.control import (
    execute_scrap_against_commitments,
    request_scrap_against_commitments,
)


def _conservation(good: int, committed: int, ledger: ScrapLedger) -> bool:
    return good == ledger.available_for_scrap(committed) + committed + ledger.pending_scrap_units + ledger.scrapped_units


def test_full_lifecycle_conserves_quantity_and_records_every_decision():
    log = EventLog()
    ledger = ScrapLedger(100, log)

    case = ledger.request("SCRAP-1", 10, "defect found", "D1", 0, 1, "alice", "E1")
    assert case.state == REQUESTED and case.version == 1
    assert ledger.pending_scrap_units == 10 and ledger.available_for_scrap(0) == 90
    assert _conservation(100, 0, ledger)

    case = ledger.review("SCRAP-1", 1, "E2", "bob", 2, note="engineering ok")
    assert case.state == ENGINEERING_REVIEWED and case.version == 2

    case = ledger.approve("SCRAP-1", 2, "E3", "carol", 3, note="approved")
    assert case.state == APPROVED and case.version == 3

    case = ledger.execute("SCRAP-1", 3, 0, "E4", "alice", 4)
    assert case.state == EXECUTED and case.version == 4
    assert ledger.scrapped_units == 10 and ledger.pending_scrap_units == 0
    assert ledger.available_for_scrap(0) == 90
    assert _conservation(100, 0, ledger)

    case = ledger.reverse("SCRAP-1", 4, "wrong diagnosis", "E5", "alice", 5)
    assert case.state == REVERSED and case.version == 5
    assert ledger.scrapped_units == 0 and ledger.available_for_scrap(0) == 100
    assert _conservation(100, 0, ledger)

    kinds = [event.kind for event in log.read("scrap:SCRAP-1")]
    assert kinds == [
        "scrap_requested",
        "scrap_reviewed",
        "scrap_approved",
        "scrap_executed",
        "scrap_reversed",
    ]
    assert [decision.version for decision in case.decisions] == [1, 2, 3, 4, 5]
    assert all(decision.allowed for decision in case.decisions)
    assert case.decisions[0].reason == "defect found"
    assert case.decisions[-1].reason == "wrong diagnosis"


def test_out_of_order_transitions_are_denied_and_never_deduct():
    ledger = ScrapLedger(100)
    ledger.request("SCRAP-1", 10, "defect", "D1", 0, 1, "alice", "E1")

    case = ledger.approve("SCRAP-1", 1, "E2", "carol", 2)
    assert not case.last_decision.allowed
    assert case.last_decision.reasons == ("invalid_state_transition:requested->approved",)
    assert case.state == REQUESTED and case.version == 1

    case = ledger.execute("SCRAP-1", 1, 0, "E3", "alice", 3)
    assert not case.last_decision.allowed
    assert case.last_decision.reasons == ("invalid_state_transition:requested->executed",)
    assert case.state == REQUESTED and ledger.scrapped_units == 0

    case = ledger.reverse("SCRAP-1", 1, "undo", "E4", "alice", 4)
    assert not case.last_decision.allowed
    assert case.last_decision.reasons == ("invalid_state_transition:requested->reversed",)
    assert ledger.scrapped_units == 0


def test_duplicate_execution_does_not_double_deduct():
    ledger = ScrapLedger(100)
    ledger.request("SCRAP-1", 10, "defect", "D1", 0, 1, "alice", "E1")
    ledger.review("SCRAP-1", 1, "E2", "bob", 2)
    ledger.approve("SCRAP-1", 2, "E3", "carol", 3)

    first = ledger.execute("SCRAP-1", 3, 0, "E4", "alice", 4)
    assert ledger.scrapped_units == 10

    replay = ledger.execute("SCRAP-1", 3, 0, "E4", "alice", 4)
    assert replay == first and ledger.scrapped_units == 10

    with pytest.raises(ValueError):
        ledger.execute("SCRAP-1", 4, 0, "E5", "alice", 5)
    assert ledger.scrapped_units == 10


def test_duplicate_reversal_does_not_double_restore():
    ledger = ScrapLedger(100)
    ledger.request("SCRAP-1", 10, "defect", "D1", 0, 1, "alice", "E1")
    ledger.review("SCRAP-1", 1, "E2", "bob", 2)
    ledger.approve("SCRAP-1", 2, "E3", "carol", 3)
    ledger.execute("SCRAP-1", 3, 0, "E4", "alice", 4)

    case = ledger.reverse("SCRAP-1", 4, "wrong", "E5", "alice", 5)
    assert case.state == REVERSED and ledger.scrapped_units == 0

    with pytest.raises(ValueError):
        ledger.reverse("SCRAP-1", 5, "wrong", "E6", "alice", 6)
    assert ledger.scrapped_units == 0


def test_stale_version_is_denied_with_current_version_evidence():
    log = EventLog()
    ledger = ScrapLedger(100, log)
    ledger.request("SCRAP-1", 10, "defect", "D1", 0, 1, "alice", "E1")

    case = ledger.review("SCRAP-1", 99, "E2", "bob", 2)
    assert not case.last_decision.allowed
    assert case.last_decision.reasons == ("stale_scrap_version",)
    assert case.state == REQUESTED and case.version == 1

    denied_event = log.read("scrap:SCRAP-1")[-1]
    assert denied_event.data["current_version"] == 1 and denied_event.data["allowed"] is False


def test_committed_units_are_protected_at_request():
    ledger = ScrapLedger(100)
    case = ledger.request("SCRAP-1", 10, "defect", "D1", 90, 1, "alice", "E1")
    assert case.units == 10 and ledger.pending_scrap_units == 10

    with pytest.raises(ValueError):
        ledger.request("SCRAP-2", 11, "defect", "D2", 90, 2, "alice", "E2")
    assert ledger.pending_scrap_units == 10


def test_committed_units_are_protected_at_execution():
    ledger = ScrapLedger(100)
    ledger.request("SCRAP-1", 10, "defect", "D1", 0, 1, "alice", "E1")
    ledger.review("SCRAP-1", 1, "E2", "bob", 2)
    ledger.approve("SCRAP-1", 2, "E3", "carol", 3)

    case = ledger.execute("SCRAP-1", 3, 95, "E4", "alice", 4)
    assert not case.last_decision.allowed
    assert "committed_units_protected" in case.last_decision.reasons
    assert ledger.scrapped_units == 0 and ledger.pending_scrap_units == 10


def test_reversal_requires_a_reason():
    ledger = ScrapLedger(100)
    ledger.request("SCRAP-1", 10, "defect", "D1", 0, 1, "alice", "E1")
    ledger.review("SCRAP-1", 1, "E2", "bob", 2)
    ledger.approve("SCRAP-1", 2, "E3", "carol", 3)
    ledger.execute("SCRAP-1", 3, 0, "E4", "alice", 4)

    with pytest.raises(ValueError):
        ledger.reverse("SCRAP-1", 4, "", "E5", "alice", 5)
    assert ledger.scrapped_units == 10


def test_scrap_id_reuse_with_different_content_raises():
    ledger = ScrapLedger(100)
    ledger.request("SCRAP-1", 10, "defect", "D1", 0, 1, "alice", "E1")
    with pytest.raises(ValueError):
        ledger.request("SCRAP-1", 20, "defect", "D2", 0, 2, "alice", "E2")


def test_event_id_reuse_with_different_content_raises():
    log = EventLog()
    ledger = ScrapLedger(100, log)
    ledger.request("SCRAP-1", 10, "defect", "D1", 0, 1, "alice", "E1")
    with pytest.raises(ValueError):
        ledger.request("SCRAP-2", 5, "defect", "D1", 0, 2, "alice", "E1")


def test_workflow_requests_scrap_while_protecting_commitments():
    commit = CommitLedger(100)
    commit.allocate("ORD-REQ-1", "ORDER-1", 60)
    scrap = ScrapLedger(100)

    case = request_scrap_against_commitments(scrap, commit, "SCRAP-1", 40, "defect", "D1", 1, "alice", "E1")
    assert case.units == 40 and scrap.pending_scrap_units == 40

    with pytest.raises(ValueError):
        request_scrap_against_commitments(scrap, commit, "SCRAP-2", 1, "defect", "D2", 2, "alice", "E2")
    assert scrap.pending_scrap_units == 40


def test_workflow_executes_scrap_against_commitments():
    commit = CommitLedger(100)
    commit.allocate("ORD-1", "ORDER-1", 30)
    scrap = ScrapLedger(100)

    request_scrap_against_commitments(scrap, commit, "SCRAP-1", 10, "defect", "D1", 1, "alice", "E1")
    scrap.review("SCRAP-1", 1, "E2", "bob", 2)
    scrap.approve("SCRAP-1", 2, "E3", "carol", 3)

    case = execute_scrap_against_commitments(scrap, commit, "SCRAP-1", 3, "E4", "alice", 4)
    assert case.state == EXECUTED and scrap.scrapped_units == 10
    assert commit.committed_units == 30
