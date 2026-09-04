# MerchantLatch

Policy-locked payments for AI agents.

MerchantLatch is a merchant-controlled safety gateway for AI-assisted checkout.
It verifies Razorpay webhooks, records accepted events durably in PostgreSQL, and dispatches recoverable work through Inngest.

## Current capabilities

- Constant-time Razorpay webhook signature verification.
- Idempotent webhook storage and transactional outbox creation.
- Restricted PostgreSQL runtime role with separate owner credentials for migrations.
- Outbox dispatch and an Inngest function mounted at `/api/inngest`.
- FastAPI liveness endpoint at `/health/live`.
- Public UCP discovery at `/.well-known/ucp`.
- Signed UCP checkout creation, update, standard cancellation, and retrieval with durable nonce and idempotency replay protection.
- SSRF-safe buyer profile resolution with persistent trust-on-first-use key pinning.
- Append-only redacted UCP exchange evidence and a token-protected JSON inspector.
- Merchant-controlled escalation handoff without creating a payment or order.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 or newer
- pnpm 11
- PostgreSQL 17 or newer
- Razorpay Test Mode credentials
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
```

`GEMINI_MODEL` defaults to `gemini-3.8-flash`.
`UCP_BUYER_PRIVATE_KEY` must be a P-256 private key in PEM format and must never be committed.
`BUYER_SESSION_SECRET` must be a random secret of at least 32 characters.
`PUBLIC_BUYER_URL` and `PUBLIC_GATEWAY_URL` must be public HTTPS origins for a complete signed checkout flow.
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
The current flow stops at `requires_escalation` and returns a verified same-origin merchant continuation URL.
It does not create a Razorpay order or initiate payment.

Run the workspace quality gates from the repository root:

```bash
pnpm lint
pnpm typecheck
pnpm test
pnpm --filter merchantlatch-buyer build
```

## Inspect redacted UCP activity

Operator-only JSON endpoints are available at `/internal/ucp/trust-pins` and `/internal/ucp/exchanges`.
Send `Authorization: Bearer <UCP_INSPECTOR_TOKEN>` and never expose this token to a browser application.
Inspector responses contain only bounded redacted metadata and digest values, never raw protocol messages or payment identifiers.
