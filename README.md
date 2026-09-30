# FabWeave Operations Lab

FabWeave is a dependency-light Python reference project for semiconductor-fab
operations.  Its production modules model queue-time protection, dispatch,
reticle reservations, recipe qualification, SPC, metrology sampling,
maintenance, chamber matching, holds, genealogy, chemical aging, cleanroom
excursions, demand control, rework, lot merging, equipment dedication,
contamination, takt, route yield, shipment commitments and controlled scrap
disposition.

The scrap domain replaces one-shot quantity edits with an event-sourced,
staged lifecycle: request, engineering review, approval, execution and a
controlled pre-execution cancellation or post-execution reversal.  Every
decision is a versioned, append-only audit event carrying its reason code,
actor and policy revision.  Out-of-order transitions, stale expected versions
and identical command redrives never deduct twice, and units already committed
to orders are never touched — scrap holds can only consume uncommitted
inventory.

The cross-domain workflow layer composes these services for material
readiness, dispatch-after-hold, qualified reservations and assignments,
metrology/SPC gates, containment, powered scheduling, rework release,
merge-to-commit and performance reporting.  It preserves domain rejection
reasons instead of replacing them with a generic success or failure.

Shared infrastructure supplies half-open time intervals, versioned policy
history, immutable decisions, quantity conservation and an append-only event
log with optimistic concurrency.  Domain services use these primitives to keep
event-time, idempotency and fail-closed behavior consistent.

Run the tests with `python -m pytest -q`.
