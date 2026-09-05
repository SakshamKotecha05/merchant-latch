# MerchantLatch

Policy-locked payments for AI agents.

MerchantLatch is a merchant-controlled safety gateway for AI-assisted checkout.
It verifies Razorpay webhooks, records accepted events durably in PostgreSQL, and dispatches recoverable work through Inngest.

## Public Test Mode deployment

- Buyer: [merchantlatch-buyer.vercel.app](https://merchantlatch-buyer.vercel.app)
- Merchant: [merchantlatch-merchant.vercel.app](https://merchantlatch-merchant.vercel.app)
- Gateway health: [merchant-latch.vercel.app/health/live](https://merchant-latch.vercel.app/health/live)
- Gateway UCP discovery: [merchant-latch.vercel.app/.well-known/ucp](https://merchant-latch.vercel.app/.well-known/ucp)

The public flow uses Razorpay Test Mode only.
No real money moves.

## Current capabilities

- Constant-time Razorpay webhook signature verification.
- Idempotent webhook storage and transactional outbox creation.
- Restricted PostgreSQL runtime role with separate owner credentials for migrations.
- Immediate outbox dispatch after approval, with an Inngest sweep as durable recovery.
- FastAPI liveness endpoint at `/health/live`.
- Public UCP discovery at `/.well-known/ucp`.
- Signed UCP checkout creation, standard update, standard cancellation, and retrieval with explicit version negotiation plus durable nonce and idempotency replay protection.
- SSRF-safe buyer profile resolution with persistent trust-on-first-use key pinning.
- Append-only redacted UCP exchange evidence and a token-protected JSON inspector.
- Merchant-controlled escalation handoff without creating a payment or order.
- Single-use merchant continuation exchange into a checkout-bound HttpOnly browser session.
- Explicit human approval before a Razorpay order or Checkout modal can be opened.
- Server-side payment verification, exact-once inventory consumption, and one merchant order.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 or newer
- pnpm 11
- PostgreSQL 17 or newer
- Razorpay Test Mode credentials (the gateway rejects Live Mode keys)
- Inngest event and signing keys

Docker is optional and can provide PostgreSQL for local development.

## Configure the gateway

Install the locked Python dependencies:

```bash
cd services/gateway
uv sync --frozen
```

Set these environment variables before starting the service:

```text
DATABASE_URL
DATABASE_DIRECT_URL
RAZORPAY_KEY_ID
RAZORPAY_KEY_SECRET
RAZORPAY_WEBHOOK_SECRET
INNGEST_EVENT_KEY
INNGEST_SIGNING_KEY
UCP_MERCHANT_PRIVATE_KEY
UCP_MERCHANT_KEY_ID
UCP_INSPECTOR_TOKEN
PUBLIC_GATEWAY_URL
PUBLIC_MERCHANT_URL
```

`DATABASE_URL` must authenticate as the restricted application role.
`DATABASE_DIRECT_URL` must authenticate as a different owner role against the same database.
Do not commit either connection URL or any provider secret.
`UCP_MERCHANT_PRIVATE_KEY` must contain the merchant's P-256 signing key and must never be committed.
`UCP_INSPECTOR_TOKEN` must be a random secret of at least 32 characters.
Buyer signing keys are fetched from the HTTPS profile URL in each signed `UCP-Agent` header and pinned after the first valid request.
`PUBLIC_GATEWAY_URL` and `PUBLIC_MERCHANT_URL` must be absolute HTTPS URLs in deployed environments.

## Prepare PostgreSQL

Start the local PostgreSQL service from the repository root if needed:

```bash
docker compose -f infra/docker-compose.yml up -d postgres
```

From `services/gateway`, provision the runtime role before applying migrations:

```bash
uv run python -m acsa.adapters.postgres.runtime_role
uv run alembic upgrade head
```

The bootstrap command is idempotent.
It grants only the table and sequence privileges required by the gateway and removes schema, temporary-table, role-creation, and migration-owner access from the runtime role.

## Run the gateway

From `services/gateway`, start FastAPI:

```bash
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

Verify the process is live:

```bash
curl http://localhost:8000/health/live
```

Expected response:

```json
{"service":"acsa-gateway","status":"alive"}
```

Configure Razorpay to send signed events to `/webhooks/razorpay` on the public gateway URL.

## Verify UCP discovery locally

After starting the gateway with valid UCP settings, fetch its profile:

```bash
curl http://localhost:8000/.well-known/ucp
```

The response advertises the REST shopping service at `/ucp/shopping` and the `dev.ucp.shopping.checkout` capability.
Local HTTP is for development only.
The public profile endpoint must use HTTPS and must not redirect.

## Run the reference buyer

Install the locked workspace dependencies from the repository root:

```bash
pnpm install --frozen-lockfile
```

Configure these server-only buyer variables:

```text
GEMINI_API_KEY
GEMINI_MODEL
UCP_BUYER_PRIVATE_KEY
UCP_BUYER_KEY_ID
BUYER_SESSION_SECRET
PUBLIC_BUYER_URL
PUBLIC_GATEWAY_URL
PUBLIC_MERCHANT_URL
```

`GEMINI_MODEL` defaults to `gemini-3.6-flash`.
`UCP_BUYER_PRIVATE_KEY` must be a P-256 private key in PEM format and must never be committed.
`BUYER_SESSION_SECRET` must be a random secret of at least 32 characters.
`PUBLIC_BUYER_URL`, `PUBLIC_GATEWAY_URL`, and `PUBLIC_MERCHANT_URL` must be public HTTPS origins for a complete signed checkout flow.
The gateway must be able to fetch `PUBLIC_BUYER_URL/.well-known/ucp` without a redirect.

Start the buyer from the repository root:

```bash
pnpm --filter merchantlatch-buyer dev
```

The buyer exposes these server routes:

- `POST /api/buyer/plan` extracts typed shopping constraints and deterministically checks the merchant catalog.
- `POST /api/buyer/plan/manual` creates the same safe plan from an exact variant selection without Gemini.
- `POST /api/buyer/checkouts` requires `confirmed: true` and a current confirmation token before sending a signed UCP checkout.
- `GET /.well-known/ucp` publishes only the buyer's public P-256 key.

Create a natural-language plan:

```bash
curl -X POST http://localhost:3000/api/buyer/plan \
  -H 'Content-Type: application/json' \
  -d '{"text":"one black running shoe in size 42 under INR 3000"}'
```

Use the deterministic manual fallback when language extraction is unavailable:

```bash
curl -X POST http://localhost:3000/api/buyer/plan/manual \
  -H 'Content-Type: application/json' \
  -d '{"variantId":"<merchant-variant-id>","quantity":1,"budgetMinor":300000,"currency":"INR"}'
```

After reviewing the returned merchant terms, submit its confirmation token explicitly:

```bash
curl -X POST http://localhost:3000/api/buyer/checkouts \
  -H 'Content-Type: application/json' \
  -d '{"confirmationToken":"<token-from-plan>","confirmed":true}'
```

The buyer re-fetches price, currency, and inventory immediately before checkout.
It accepts merchant checkout data only after verifying the response signature and UCP schema.
The buyer handoff stops at `requires_escalation` and returns a verified same-origin merchant continuation URL.
It does not create a Razorpay order or initiate payment.

Run the workspace quality gates from the repository root:

```bash
pnpm lint
pnpm typecheck
pnpm test
pnpm --filter merchantlatch-buyer build
pnpm --filter merchantlatch-merchant build
```

Run the tracked-only clean-clone certification when PostgreSQL 17 tools are installed:

```bash
./scripts/verify-clean-clone.sh
```

The verifier clones the current committed revision into a temporary directory, creates an isolated PostgreSQL cluster, round-trips every migration, provisions the restricted runtime role, and runs every static check, test suite, and production build.

## Inspect redacted UCP activity

Operator-only JSON endpoints are available at `/internal/ucp/trust-pins` and `/internal/ucp/exchanges`.
Send `Authorization: Bearer <UCP_INSPECTOR_TOKEN>` and never expose this token to a browser application.
Inspector responses contain only bounded redacted metadata and digest values, never raw protocol messages or payment identifiers.

## Run the merchant checkout

The merchant application is separate from the reference buyer.
It exchanges a signed continuation once for a checkout-scoped, HttpOnly browser session, then redirects to a clean review URL.
The merchant gateway checks explicit consent, the exact approval snapshot, expiry, stock, and payment-attempt ownership before allowing payment.
Razorpay Checkout opens only after the shopper chooses to pay.
A successful browser callback is verified against provider data; it cannot mark an order paid by itself.

Set these variables on the merchant Next.js server:

```text
PUBLIC_GATEWAY_URL
PUBLIC_MERCHANT_URL
```

Use the same merchant origin on the gateway and merchant application.
The local merchant UI defaults to `http://127.0.0.1:8000` and `http://localhost:3001` for isolated development.
The gateway requires HTTPS public origins even when its process runs locally, so a complete signed buyer-to-merchant journey needs HTTPS development entrypoints.
Production requires HTTPS origins without paths, query parameters, or embedded credentials.
The merchant application uses Secure, HttpOnly cookies in production and does not expose session credentials to client-side JavaScript.

From the repository root:

```bash
pnpm --filter merchantlatch-merchant dev
```

Open a fresh merchant continuation from the buyer to review and approve a purchase.
The merchant displays pending verification, expiry, refund, and manual-review states instead of claiming success prematurely.
A completed checkout exposes a minimal order-confirmation permalink containing only an unguessable order identifier, status, amount, and currency.
Treat that permalink as private.
Order confirmation stays in the application; this version collects no email address and sends no confirmation email.
Payment evidence and the merchant order are recorded atomically with completion, without deferred confirmation or evidence jobs.

### Merchant operator access

Set `MERCHANT_ADMIN_PASSWORD_HASH` on the gateway to an Argon2id PHC-encoded password hash.
When it is absent, operator sign-in is disabled.
Generate the hash locally from `services/gateway`; the password is read without echo:

```bash
uv run python - <<'PY'
from getpass import getpass
from secrets import token_bytes
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
password = getpass("Merchant operator password: ").encode()
if len(password) < 16 or len(password) > 256:
    raise SystemExit("Use a password between 16 and 256 bytes.")
print(Argon2id(salt=token_bytes(16), length=32, iterations=3,
               lanes=1, memory_cost=65536).derive_phc_encoded(password))
PY
```

Store the hash as a secret environment value, never in the repository.
Visit `/operator` on the merchant application to inspect recent checkout states, redacted audit activity, protocol digests, and the background-work queue.
Sign-in allows five attempts per minute across the single operator account.
Operator sessions expire after one hour and are revoked on sign-out.

### Verification status

The tracked-only clean-clone verifier passes against PostgreSQL 17.
It records 348 passing gateway tests, 150 passing buyer tests with one optional live-provider smoke test skipped, and 16 passing merchant tests.
Gateway Ruff lint, formatting, strict mypy, frontend ESLint, frontend TypeScript, both production builds, and the full migration upgrade, downgrade, and re-upgrade also pass.

The public buyer, merchant, and gateway applications are deployed on Vercel.
A deployed browser run completed the natural-language request, deterministic catalog match, signed UCP handoff, one-time merchant session exchange, explicit approval, Inngest provider-order creation, Razorpay Test Mode wallet payment, webhook processing, and final merchant confirmation.
Independent database inspection found checkout status `completed`, payment attempt state `paid`, webhook evidence, a consumed inventory lease, exactly one merchant order, zero remaining reservation, and one sold unit.
Exactly one `payment.captured` event and one `order.paid` event were stored and processed for that order.
A second deployed browser run verified that approval reached payment-ready state in about four seconds through immediate dispatch, rather than waiting for the one-minute recovery sweep.

Deployed failure checks also confirmed that cancelled Test Mode payments, a rejected international test card, and an expired inventory lease do not create a merchant order or show a false payment success.

The frozen deterministic held-out language baseline evaluated 50 language cases without Gemini.
It achieved 76.47% required-constraint exact-field accuracy, 73.53% constraint-satisfying cart accuracy, 66.67% clarification precision, 87.50% clarification recall, a 2.00% parser block rate, and no schema failures.

The separate PostgreSQL-backed held-out safety run evaluated all 50 frozen scenario cases through local production boundaries.
It recorded no injection escapes and no captured-payment counter mismatches.
It recorded three conservative provider-order counter mismatches for malformed provider POST responses and seven conflicts with the corpus's blanket no-provider-actuation label.
Four of those actuation conflicts occur in reliability fixtures that also expect a provider artifact, so the result is retained as an evaluation-contract limitation rather than presented as a production safety pass.

A redacted Ed25519 evidence bundle containing both final reports and their freeze manifests verifies offline.
Independent altered-file and wrong-key checks reject the bundle as expected.

Pinned external UCP conformance was run locally against the production router boundary.
The raw suite's placeholder request signature was correctly rejected by MerchantLatch's mandatory P-256 authentication.
A local-only signing bridge then supplied valid ephemeral signatures without bypassing production verification.
The authenticated capability-scoped core passed 15 tests with two upstream skips, covering discovery, version negotiation, create/get/cancel lifecycle, canceled-state rejection, create/update/cancel idempotency, and total structure.
The unchanged 77-test full suite passed 19, failed 30, and skipped 28 because it also exercises payment completion and unadvertised discount, buyer-data, fulfillment, order, webhook, and simulation surfaces.

A historical live `gemini-3.8-flash` run evaluated all 50 frozen held-out language cases through the stateless production client.
Forty-five calls were classified as provider-unavailable under the former five-second production deadline.
After bounded retries and a longer hard deadline were added, an authorized live smoke request passed; the full 50-case live run was not repeated.
The resulting low aggregate score is retained as availability-limited evidence and does not establish stable live-model accuracy.

The deployed buyer now uses `gemini-3.6-flash` because the configured account had exhausted quota for the former model.
A fresh live extraction smoke check passed with the deployed model.

The deployment proves the complete product in Razorpay Test Mode.
It does not claim live-money certification, complete support for unadvertised optional UCP capabilities, or measured production load capacity.
