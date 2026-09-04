# Phase 7 Reliability, Conformance, and Repository Consolidation Design

## Status

Approved in conversation on 2026-09-05.

## Goal

Complete the remaining Phase 7 production work by improving Gemini request resilience, closing required UCP checkout protocol gaps, verifying the full local database-backed architecture, and consolidating every branch into one verified `main` branch.

## Scope

The production scope includes the reference buyer, the signed UCP gateway, PostgreSQL persistence, merchant checkout, product tests, migrations, required configuration, and canonical architecture documentation.

The repository scope includes runnable source, product tests, migrations, lockfiles, `README.md`, and this specification.

Local evaluation corpora, raw reports, evidence bundles, checkpoint handoffs, execution plans, temporary harnesses, credentials, caches, and generated build output remain excluded from version control.

## Gemini reliability

The production extractor will continue to use the Gemini Interactions API with structured JSON output and `store: false`.

The current five-second single-attempt policy is too aggressive for observed provider behavior.

A secret-safe probe completed successfully in 2.7 seconds, while the frozen burst run produced transient provider failures and one five-second connection timeout.

The SDK request policy will use a bounded retry allowance for retryable transport failures and provider throttling.

The request will retain an absolute deadline so provider slowness cannot hold a buyer request indefinitely.

Caller cancellation must stop all retry activity immediately.

Provider error text, raw model output, prompts, and credentials must never appear in public error responses or logs.

Schema validation remains mandatory after every successful provider response.

The existing deterministic manual planning path remains the safe fallback when Gemini is unavailable.

## UCP capability boundary

MerchantLatch advertises `dev.ucp.shopping.checkout` and no payment handler, fulfillment, discount, buyer-consent, order, simulation, AP2, token-binding, or webhook capability.

Phase 7 will implement only protocol behavior required for the advertised checkout capability and universal UCP transport rules.

Failures caused solely by unadvertised optional capabilities remain disclosed conformance exclusions rather than targets for placeholder implementations.

MerchantLatch will not advertise a capability until its state model, persistence, security boundary, and end-to-end tests are complete.

## UCP version negotiation

`UCP-Agent` parsing will accept one canonical `profile` member with an optional `version` parameter.

The only accepted explicit version is the date-based version advertised by MerchantLatch discovery.

An incompatible explicit version must return HTTP 422 with a stable `version_unsupported` error before profile retrieval or commerce mutation.

A missing version parameter remains compatible because the buyer profile itself declares and cryptographically binds its UCP version.

The buyer profile must still use HTTPS, pass SSRF controls, publish the matching UCP version, and provide the key used to verify the request signature.

Version rejection must not weaken signature verification for compatible requests.

## Standard checkout updates

The update endpoint will accept a standard UCP checkout update without MerchantLatch's private `expected_version` extension.

When `expected_version` is absent, the gateway will load the buyer-owned checkout and use its current version as the optimistic concurrency precondition.

The database compare-and-swap remains authoritative, so concurrent updates cannot silently overwrite each other.

An explicitly supplied positive `expected_version` remains supported for existing MerchantLatch clients.

The gateway will continue to reject malformed line items, nonpositive quantities, unsupported currency, invalid price hints, missing checkouts, cross-buyer access, stale versions, and terminal-state mutations.

Payment, fulfillment, buyer, and discount fields may be ignored only when they belong to unadvertised capabilities and do not contradict merchant-authoritative line items or currency.

## Idempotency precedence

Idempotency is evaluated for an authenticated buyer before request semantics that can vary between retries.

The durable idempotency record is scoped by buyer identity, operation, resource when applicable, and idempotency key.

The record stores a request digest and the resulting status and canonical response needed for exact replay.

The same key and digest returns the original response without repeating commerce or provider effects.

The same key with a different digest returns HTTP 409 even when the modified payload would otherwise fail field validation.

New invalid requests must not create idempotency records, checkouts, payment attempts, provider orders, or audit claims of acceptance.

The existing nonce replay and checkout version controls remain independent defenses.

## Error and evidence behavior

Every protocol error uses the existing bounded UCP error envelope.

Authentication failures remain HTTP 401, unsupported versions use HTTP 422, idempotency conflicts and stale versions use HTTP 409, invalid request shapes use HTTP 400 or 422 as appropriate, and unavailable internal dependencies use HTTP 500 without internal details.

Redacted exchange evidence records the route, outcome, status, hashes, buyer identity metadata, and timing without raw bodies, authorization material, payment identifiers, or secrets.

## Verification

Development follows test-driven development with an end-user-aligned failing route test before each production fix.

Gemini tests cover structured output, retry configuration, absolute cancellation, invalid output, provider failure redaction, and the manual fallback.

UCP tests cover compatible, absent, malformed, and incompatible version declarations; standard updates without `expected_version`; explicit optimistic versions; durable replay; changed-payload conflicts; cross-buyer isolation; and zero commerce action after rejected versions.

The pinned official UCP suite will be rerun through the valid local signing bridge.

Results will distinguish universal and advertised-checkout tests from tests that require unadvertised optional capabilities.

The complete PostgreSQL-backed gateway suite, buyer suite, merchant browser tests, lint, type checking, and production builds must pass before consolidation.

No live payment, capture, or refund is part of verification.

## Repository consolidation

Phase 6 is the implementation worktree because it contains the current uncommitted merchant and gateway work on top of `origin/main`.

The current local `main` history and every topic-branch history will remain reachable from the final `main` commit.

Superseded phase branches whose committed trees are older than `origin/main` will be record-merged without replacing verified files with obsolete versions.

The verified Phase 6 implementation will be merged normally.

A local Git bundle will be created outside the repository before branch deletion.

After the final tree and history are verified, `main` will be pushed and every other local and remote branch will be deleted.

Auxiliary worktrees will be detached before their branch references are deleted unless the user separately requests removal of their directories.

## Acceptance criteria

- Gemini extraction has bounded retry behavior, an absolute deadline, safe cancellation, schema enforcement, and no secret reflection.
- Unsupported explicit UCP versions return HTTP 422 without commerce mutation.
- Standard UCP checkout updates work without a private version field while retaining optimistic concurrency.
- Durable idempotency replays identical requests and rejects changed payloads before semantic validation or side effects.
- MerchantLatch advertises only implemented capabilities.
- Required local database-backed, buyer, merchant, lint, type, and build gates pass.
- The final repository contains only runnable architecture, product tests, migrations, required configuration, lockfiles, README, and canonical specifications.
- All branch histories are reachable from `main`, the remote `main` is verified, and no other local or remote branch remains.
- No credential, local report, evaluation corpus, evidence key, cache, or generated build output is committed.
