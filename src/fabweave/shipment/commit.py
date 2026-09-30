from __future__ import annotations

from dataclasses import dataclass


def committed_units(
    good_units: int,
    reserved_units: int,
    safety_units: int,
    pending_scrap: int = 0,
    quarantine_units: int = 0,
) -> int:
    values = (good_units, reserved_units, safety_units, pending_scrap, quarantine_units)
    if any(not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("shipment quantities must be non-negative integers")
    return max(0, good_units - reserved_units - safety_units - pending_scrap - quarantine_units)


@dataclass(frozen=True, slots=True)
class Allocation:
    request_id: str
    order_id: str
    units: int


class CommitLedger:
    def __init__(self, good_units: int, safety_units: int = 0) -> None:
        if min(good_units, safety_units) < 0:
            raise ValueError("negative inventory")
        self._good = good_units
        self._safety = safety_units
        self._pending_scrap = 0
        self._quarantine = 0
        self._allocations: dict[str, Allocation] = {}

    def mark_unavailable(self, pending_scrap: int, quarantine: int) -> None:
        if min(pending_scrap, quarantine) < 0:
            raise ValueError("negative unavailable quantity")
        reserved = sum(item.units for item in self._allocations.values())
        if pending_scrap + quarantine + reserved + self._safety > self._good:
            raise ValueError("unavailable quantity exceeds uncommitted inventory")
        self._pending_scrap = pending_scrap
        self._quarantine = quarantine

    def allocate(self, request_id: str, order_id: str, units: int) -> Allocation:
        existing = self._allocations.get(request_id)
        candidate = Allocation(request_id, order_id, units)
        if existing is not None:
            if existing != candidate:
                raise ValueError("request id reused with different allocation")
            return existing
        reserved = sum(item.units for item in self._allocations.values())
        available = committed_units(self._good, reserved, self._safety, self._pending_scrap, self._quarantine)
        if units <= 0 or units > available:
            raise ValueError("insufficient shippable inventory")
        self._allocations[request_id] = candidate
        return candidate

    @property
    def available(self) -> int:
        reserved = sum(item.units for item in self._allocations.values())
        return committed_units(self._good, reserved, self._safety, self._pending_scrap, self._quarantine)

    @property
    def committed_units(self) -> int:
        return sum(item.units for item in self._allocations.values())

    @property
    def good_units(self) -> int:
        return self._good
