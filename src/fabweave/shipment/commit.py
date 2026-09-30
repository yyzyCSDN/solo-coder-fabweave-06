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
        # Per-request scrap reservations: units are held (unshippable) but not
        # yet removed from good inventory.
        self._scrap_holds: dict[str, int] = {}
        # Requests whose scrap has been executed, mapped to the scrapped quantity.
        self._scrapped: dict[str, int] = {}
        self._scrapped_total = 0

    def mark_unavailable(self, pending_scrap: int, quarantine: int) -> None:
        if min(pending_scrap, quarantine) < 0:
            raise ValueError("negative unavailable quantity")
        reserved = self._order_reserved()
        if (
            pending_scrap
            + quarantine
            + reserved
            + self._safety
            + self._pending_scrap_holds()
            + self._scrapped_total
            > self._good
        ):
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
        reserved = self._order_reserved()
        available = committed_units(
            self._good, reserved, self._safety, self.pending_scrap + self._scrapped_total, self._quarantine
        )
        if units <= 0 or units > available:
            raise ValueError("insufficient shippable inventory")
        self._allocations[request_id] = candidate
        return candidate

    def reserve_scrap(self, request_id: str, units: int) -> int:
        """Hold uncommitted units for a scrap request; idempotent per request."""
        existing = self._scrap_holds.get(request_id)
        if existing is not None:
            if existing != units:
                raise ValueError("scrap request id reused with different quantity")
            return existing
        if units <= 0:
            raise ValueError("scrap quantity must be positive")
        if units > self.scrap_room_for(request_id):
            raise ValueError("scrap quantity exceeds uncommitted inventory")
        self._scrap_holds[request_id] = units
        return units

    def adjust_scrap_reservation(self, request_id: str, units: int) -> int:
        """Change the held quantity for an existing scrap reservation."""
        if request_id not in self._scrap_holds:
            raise ValueError("unknown scrap request")
        if units <= 0:
            raise ValueError("scrap quantity must be positive")
        if units > self.scrap_room_for(request_id):
            raise ValueError("scrap quantity exceeds uncommitted inventory")
        self._scrap_holds[request_id] = units
        return units

    def release_scrap(self, request_id: str) -> int:
        """Release a scrap reservation before execution (cancellation)."""
        units = self._scrap_holds.pop(request_id, None)
        if units is None:
            raise ValueError("unknown scrap request")
        return units

    def confirm_scrap(self, request_id: str) -> int:
        """Move a reservation out of good inventory exactly once."""
        if request_id in self._scrapped:
            return 0
        units = self._scrap_holds.pop(request_id, None)
        if units is None:
            raise ValueError("unknown scrap request")
        self._scrapped[request_id] = units
        self._scrapped_total += units
        return units

    def reverse_scrap(self, request_id: str, units: int) -> int:
        """Move units from an executed scrap back into shippable good inventory."""
        held = self._scrapped.get(request_id)
        if held is None:
            raise ValueError("scrap request was not executed")
        if not isinstance(units, int) or units <= 0 or units > held:
            raise ValueError("reversal quantity outside executed scrap")
        self._scrapped_total -= units
        if units == held:
            del self._scrapped[request_id]
        else:
            self._scrapped[request_id] = held - units
        return units

    def scrap_room_for(self, request_id: str) -> int:
        """Uncommitted units available to hold for a request, excluding its own hold."""
        others = sum(units for key, units in self._scrap_holds.items() if key != request_id)
        used = (
            self._safety
            + self._order_reserved()
            + self._pending_scrap
            + self._quarantine
            + others
            + self._scrapped_total
        )
        return max(0, self._good - used)

    def _order_reserved(self) -> int:
        return sum(item.units for item in self._allocations.values())

    def _pending_scrap_holds(self) -> int:
        return sum(self._scrap_holds.values())

    @property
    def available(self) -> int:
        reserved = self._order_reserved()
        return committed_units(self._good, reserved, self._safety, self.pending_scrap + self._scrapped_total, self._quarantine)

    @property
    def pending_scrap(self) -> int:
        """Units held by pending scrap, including legacy mark_unavailable holds."""
        return self._pending_scrap + self._pending_scrap_holds()

    @property
    def scrapped(self) -> int:
        return self._scrapped_total
