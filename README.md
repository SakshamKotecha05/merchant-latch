# MerchantLatch

**AI-assisted shopping. Merchant-controlled terms. Human-approved payments.**

MerchantLatch turns a shopper’s request into a verified merchant checkout.
The reference buyer finds a matching sneaker, checks the shopper’s constraints, and hands off signed purchase terms to the merchant.
The shopper reviews and approves those terms before Razorpay Checkout can open.

[Try the buyer](https://merchantlatch-buyer.vercel.app) · [Merchant checkout](https://merchantlatch-merchant.vercel.app) · [Gateway health](https://merchant-latch.vercel.app/health/live) · [UCP discovery](https://merchant-latch.vercel.app/.well-known/ucp)

> This is a Razorpay Test Mode demo.
> No real money moves, and the gateway rejects Live Mode keys.

![MerchantLatch buyer workspace with a shopping request form, purchase progress, and merchant verification checks](docs/images/buyer-workspace.png)

*Actual deployed buyer interface, captured September 5, 2026.*

## Try it

1. Open the [reference buyer](https://merchantlatch-buyer.vercel.app).
2. Choose an example request, such as “One black Stride One in size UK 9 under INR 6,000,” then select **Find merchant match**.
3. Review the matched product, quantity, price, and budget.
4. Confirm the terms to follow the verified merchant handoff.
5. Review and approve the purchase on the merchant site before opening Razorpay Test Mode checkout.

Use **Use exact variant instead** if language extraction is unavailable.
Start each purchase in the buyer; merchant continuation links are private, expire, and can only be exchanged once.

## What it does

- **Constrained shopping:** Gemini extracts shopping requirements; deterministic catalog checks validate product, quantity, budget, price, and stock.
- **Signed handoff:** Universal Commerce Protocol (UCP) requests and responses use P-256 signatures, version negotiation, and replay protection.
- **Explicit consent:** the buyer cannot initiate payment; the merchant requires a current approval snapshot before creating a Razorpay order.
- **Verified completion:** server-side provider verification, signed webhooks, and transactional writes control payment status, inventory consumption, and merchant order creation.
- **Recoverable work:** a PostgreSQL transactional outbox dispatches jobs through Inngest, with a periodic recovery sweep.
- **Operator visibility:** authenticated merchant tools expose checkout states, redacted audit activity, and background work.

## Architecture

```text
Shopper request
      |
      v
Buyer app (Next.js + Gemini)
      | signed UCP checkout
      v
Gateway (FastAPI + PostgreSQL)
      | verified continuation
      v
Merchant app (Next.js)
      | explicit shopper approval
      v
Gateway + Inngest -> Razorpay Test Mode
      | provider verification + signed webhooks
      v
Recorded payment, inventory update, and merchant order
```

| Component | Location | Responsibility |
| --- | --- | --- |
| Reference buyer | `apps/buyer` | Request entry, catalog matching, signed checkout handoff |
| Merchant checkout | `apps/merchant` | Purchase review, consent, payment UI, operator access |
| Gateway | `services/gateway` | UCP, commerce rules, payment verification, database migrations |
| Local infrastructure | `infra` | Docker Compose services |
| Verification | `scripts/verify-clean-clone.sh` | Isolated database checks, tests, and production builds |

The frontend uses Next.js 16, React 19, and TypeScript.
The gateway uses Python 3.12, FastAPI, SQLAlchemy, and PostgreSQL, with Inngest for background work.

## Development setup

### Prerequisites

- Node.js 22 or newer and pnpm **11.19.0** (pinned in `package.json`).
- Python **3.12** and [uv](https://docs.astral.sh/uv/).
- PostgreSQL **17 or newer**, or Docker for the included PostgreSQL service.
- Razorpay **Test Mode** credentials and a webhook secret.
- Inngest event and signing keys.
- A Gemini API key for natural-language extraction.
- Public HTTPS entrypoints for the full signed buyer-to-merchant flow.

Local HTTP servers can run behind HTTPS development tunnels.
The gateway requires HTTPS public URLs even when its process runs locally, and it must be able to fetch the buyer’s public profile without redirects.

### 1. Install dependencies

From the repository root:

```bash
pnpm install --frozen-lockfile
cd services/gateway
uv sync --frozen
cd ../..
```

### 2. Configure the services

Use [`.env.example`](.env.example) as a variable reference.
Keep real credentials in ignored local environment files or your deployment platform’s secret settings.
A root `.env` is not automatically loaded by all three services.
Export gateway variables into its shell, and use app-specific `.env.local` files for Next.js.

| Service | Required configuration |
| --- | --- |
| Gateway | `DATABASE_URL`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `INNGEST_EVENT_KEY`, `INNGEST_SIGNING_KEY`, `UCP_MERCHANT_PRIVATE_KEY`, `UCP_MERCHANT_KEY_ID`, `UCP_INSPECTOR_TOKEN`, `PUBLIC_GATEWAY_URL`, `PUBLIC_MERCHANT_URL` |
| Database provisioning and migrations | `DATABASE_DIRECT_URL`, plus `DATABASE_URL` for runtime-role provisioning |
| Buyer | `GEMINI_API_KEY`, `UCP_BUYER_PRIVATE_KEY`, `UCP_BUYER_KEY_ID`, `BUYER_SESSION_SECRET`, `PUBLIC_BUYER_URL`, `PUBLIC_GATEWAY_URL`, `PUBLIC_MERCHANT_URL` |
| Merchant | `PUBLIC_GATEWAY_URL`, `PUBLIC_MERCHANT_URL` |
| Optional operator sign-in | `MERCHANT_ADMIN_PASSWORD_HASH` on the gateway |

`DATABASE_URL` must use a restricted application role.
`DATABASE_DIRECT_URL` must use a different owner role against the same database.
For the included local Docker database, use:

```bash
export DATABASE_DIRECT_URL='postgresql+psycopg://acsa_owner:local-only-owner-password@127.0.0.1:5432/acsa'
export DATABASE_URL='postgresql+psycopg://acsa_runtime:local-only-runtime-password@127.0.0.1:5432/acsa'
```

These passwords are for local development only.
Use independent P-256 PEM private keys for `UCP_MERCHANT_PRIVATE_KEY` and `UCP_BUYER_PRIVATE_KEY`, each with its own key ID.
Generate a key locally with `openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256`, then store its complete multiline output in the appropriate secret variable.
`UCP_INSPECTOR_TOKEN` and `BUYER_SESSION_SECRET` must each contain at least 32 characters; `openssl rand -hex 32` generates a suitable value.
Never use `NEXT_PUBLIC_` for these secrets.

`GEMINI_MODEL` is optional and defaults to `gemini-3.5-flash-lite` in the buyer code.
Set it explicitly if your provider account uses another supported model.
Use consistent public origins across the three services, without paths, query strings, or embedded credentials.
The buyer publishes its public signing key at `PUBLIC_BUYER_URL/.well-known/ucp`.

### 3. Prepare the database

From the repository root:

```bash
docker compose -f infra/docker-compose.yml up -d postgres
cd services/gateway
uv run alembic upgrade head
uv run python -m acsa.adapters.postgres.runtime_role
cd ../..
```

Migrations create the schema and demo catalog.
The idempotent role bootstrap grants the runtime user the required privileges while restricting owner and schema access.

### 4. Start the apps

Run each command in a separate terminal, with that service’s environment configured:

```bash
# Terminal 1, from services/gateway
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

```bash
# Terminal 2, from the repository root
pnpm --filter merchantlatch-buyer dev
```

```bash
# Terminal 3, from the repository root
pnpm --filter merchantlatch-merchant dev
```

The local buyer runs at `http://localhost:3000`, the merchant at `http://localhost:3001`, and the gateway at `http://localhost:8000`.
For a complete purchase flow, browse through the HTTPS entrypoints configured above.
Connect Inngest to the gateway’s `/api/inngest` endpoint, and configure Razorpay Test Mode webhooks to reach `/webhooks/razorpay` on the public gateway.
Background payment work requires a working Inngest connection.

Check gateway liveness:

```bash
curl http://localhost:8000/health/live
# {"service":"acsa-gateway","status":"alive"}
```

Liveness confirms the process is running; it does not certify database or provider connectivity.

## Checks and tests

Frontend checks, from the repository root:

```bash
pnpm lint
pnpm typecheck
pnpm test
pnpm --filter merchantlatch-buyer build
pnpm --filter merchantlatch-merchant build
```

Gateway checks, from `services/gateway`:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Gateway integration tests require `TEST_DATABASE_URL` and `TEST_DATABASE_DIRECT_URL` pointing at a dedicated test database; otherwise those tests skip.
Do not point test configuration at a shared or production database.

For isolated verification of the **current committed revision**, run from the repository root:

```bash
./scripts/verify-clean-clone.sh
```

This requires local PostgreSQL 17+ command-line tools, including `pg_config`, `initdb`, `pg_ctl`, and `createdb`.
It creates a temporary checkout and database, checks migration upgrade/downgrade/re-upgrade, provisions the runtime role, and runs static checks, tests, and both production builds.
Uncommitted changes are not included.

## API and operator access

| Endpoint | Purpose |
| --- | --- |
| Gateway `GET /health/live` | Process liveness |
| Gateway `GET /.well-known/ucp` | Public merchant capabilities and signing keys |
| Gateway `/ucp/shopping` | Signed checkout create, retrieve, update, and cancel operations |
| Gateway `POST /webhooks/razorpay` | Verified Razorpay events |
| Buyer `POST /api/buyer/plan` | Natural-language shopping plan |
| Buyer `POST /api/buyer/plan/manual` | Deterministic plan from an exact variant |
| Buyer `POST /api/buyer/checkouts` | Confirmed, signed merchant handoff |
| Buyer `GET /.well-known/ucp` | Public buyer signing key |

Visit `/operator` on the merchant app for operator sign-in.
It is disabled unless `MERCHANT_ADMIN_PASSWORD_HASH` contains an Argon2id PHC-encoded password hash on the gateway.

<details>
<summary>Generate an operator password hash</summary>

Run from `services/gateway` and store the output as a secret environment value:

```bash
uv run python - <<'PYTHON'
from getpass import getpass
from secrets import token_bytes
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

password = getpass("Merchant operator password: ").encode()
if len(password) < 16 or len(password) > 256:
    raise SystemExit("Use a password between 16 and 256 bytes.")
print(Argon2id(salt=token_bytes(16), length=32, iterations=3,
               lanes=1, memory_cost=65536).derive_phc_encoded(password))
PYTHON
```

</details>

Gateway endpoints `/internal/ucp/trust-pins` and `/internal/ucp/exchanges` provide redacted protocol metadata.
They require `Authorization: Bearer <UCP_INSPECTOR_TOKEN>`; keep that token server-side.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Gateway rejects startup configuration | Required variables are exported, the Razorpay key starts with `rzp_test_`, and public gateway/merchant URLs use HTTPS |
| Signed handoff fails | Buyer discovery is reachable over HTTPS without redirects, signing keys match their published profiles, and all public origins agree |
| Gemini is unavailable | Check API quota and `GEMINI_MODEL`, or choose the exact-variant fallback |
| Payment stays pending after approval | Check Inngest connectivity and the operator background-work queue |
| Payment is awaiting verification | Check Razorpay webhook delivery and server-side provider verification |
| Checkout link is expired or already used | Start a fresh handoff from the buyer |

## Scope and limitations

The demo supports the advertised UCP checkout capability, not every optional UCP extension.
It is not live-money certified, and production load capacity has not been established.
Language-model extraction depends on provider availability; deterministic checks still govern the accepted cart.
Order confirmation appears in the application; this version does not collect email addresses or send confirmation emails.
Treat order-confirmation permalinks as private.
