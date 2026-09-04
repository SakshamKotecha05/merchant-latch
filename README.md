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

## Inspect redacted UCP activity

Operator-only JSON endpoints are available at `/internal/ucp/trust-pins` and `/internal/ucp/exchanges`.
Send `Authorization: Bearer <UCP_INSPECTOR_TOKEN>` and never expose this token to a browser application.
Inspector responses contain only bounded redacted metadata and digest values, never raw protocol messages or payment identifiers.
