# MerchantLatch Phase 1 - Thin UCP Checkout Slice

## Purpose

MerchantLatch Phase 1 establishes the smallest externally usable UCP checkout journey while preserving the Phase 0 payment-safety boundary.
The slice proves protocol discovery, authenticated mutation, durable replay protection, and a merchant-controlled escalation handoff.
It deliberately does not initiate a Razorpay payment, reserve inventory, or complete an order.

## Scope

The gateway publishes `GET /.well-known/ucp` with a static MerchantLatch discovery document that advertises the shopping capability and the checkout API base URL.
The gateway accepts a signed UCP checkout-creation request at the protocol-defined checkout endpoint.
The gateway verifies the exact raw body digest, request signature, expected buyer key identifier, nonce, and request expiry before accepting a mutation.
The gateway atomically persists the checkout resource, accepted nonce, and idempotency record.
The gateway responds with `requires_escalation`, a UCP error message with `requires_buyer_review` severity, and an absolute HTTPS `continue_url` controlled by the merchant.
The gateway serves the persisted resource through a signed checkout retrieval endpoint.

## Boundaries

The existing Phase 0 Razorpay webhook ledger and outbox remain unchanged.
No credentials, private keys, golden signatures, or provider identifiers enter source control.
The runtime role receives only the permissions needed by the new checkout tables.
Creation must be replay-safe before any external side effect exists, so a matching idempotency key returns the original immutable response and a reused nonce is rejected.
The initial state is intentionally escalation-only, so no new path can claim `complete_in_progress` or `completed`.

## Data Model

One checkout row stores an opaque UUID, creation and update timestamps, the terminal-safe `requires_escalation` state, an expiry timestamp, the serialized UCP resource, and the merchant handoff URL.
One nonce row records the buyer key identifier, nonce, expiry, and checkout association.
One idempotency row records the buyer key identifier, idempotency key, request digest, and checkout association.
Unique constraints on buyer key plus nonce and buyer key plus idempotency key are the replay boundary.

## API Behavior

Discovery is public and returns only non-sensitive static capability metadata.
Checkout creation rejects malformed JSON, missing protocol fields, bad content digests, invalid or expired signatures, nonce replays, request-expiry violations, and conflicting idempotency keys with protocol-safe client errors.
Checkout retrieval verifies the same signature boundary before returning a known checkout.
Unknown checkout IDs return a protocol-safe not-found response without leaking database details.
All accepted creation responses are stored and replayed byte-for-byte from the durable checkout representation.

## Testing

Contract tests cover discovery shape, valid creation, exact replay, nonce replay rejection, idempotency conflict rejection, invalid signatures, invalid content digests, and signed retrieval.
Database integration tests cover atomic persistence, unique-constraint behavior, and runtime-role permissions.
The focused suite runs before the full Ruff format check, Ruff lint, strict mypy, non-integration pytest, and PostgreSQL integration pytest checks.

## Deferred Work

Buyer-profile fetching and structured `UCP-Agent` profile validation are deferred.
Checkout update, cancellation, expiry materialization, inventory consumption, Razorpay payment creation, payment verification, merchant-order persistence, and UCP completion are deferred.
The `complete_in_progress`, `completed`, and late-capture refund paths are deferred.

## Acceptance Criteria

An official-compatible discovery probe can locate MerchantLatch's shopping capability.
A signed buyer can create and retrieve one durable escalated checkout without any payment action.
An exact creation replay returns the original resource without creating another checkout.
Reused nonces and conflicting idempotency keys fail closed.
No unsigned request can read or mutate a checkout resource.
All existing Phase 0 checks remain green.
