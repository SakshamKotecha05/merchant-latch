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
```

`DATABASE_URL` must authenticate as the restricted application role.
`DATABASE_DIRECT_URL` must authenticate as a different owner role against the same database.
Do not commit either connection URL or any provider secret.

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
