#!/usr/bin/env bash

set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
verification_root="$(mktemp -d "${TMPDIR:-/tmp}/merchantlatch-verify.XXXXXX")"
checkout_root="${verification_root}/checkout"
postgres_data="${verification_root}/postgres"
postgres_socket="${verification_root}/socket"
postgres_log="${verification_root}/postgres.log"
postgres_port="${MERCHANTLATCH_VERIFY_POSTGRES_PORT:-$((55000 + RANDOM % 5000))}"
postgres_started=false

cleanup() {
  if [[ "${postgres_started}" == "true" ]]; then
    "${pg_bin}/pg_ctl" -D "${postgres_data}" -m fast -w stop >/dev/null
  fi
  rm -rf "${verification_root}"
}
trap cleanup EXIT INT TERM

for command_name in git node pnpm uv pg_config; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${command_name}" >&2
    exit 1
  fi
done

pg_bin="$(pg_config --bindir)"
for postgres_command in initdb pg_ctl createdb; do
  if [[ ! -x "${pg_bin}/${postgres_command}" ]]; then
    echo "Required PostgreSQL command is unavailable: ${pg_bin}/${postgres_command}" >&2
    exit 1
  fi
done

postgres_major="$(${pg_bin}/pg_config --version | sed -E 's/^PostgreSQL ([0-9]+).*/\1/')"
if [[ ! "${postgres_major}" =~ ^[0-9]+$ ]] || (( postgres_major < 17 )); then
  echo "PostgreSQL 17 or newer is required" >&2
  exit 1
fi

mkdir -p "${postgres_socket}"
"${pg_bin}/initdb" \
  -D "${postgres_data}" \
  --username=acsa_owner \
  --auth=trust \
  --no-instructions >/dev/null
"${pg_bin}/pg_ctl" \
  -D "${postgres_data}" \
  -l "${postgres_log}" \
  -o "-h 127.0.0.1 -p ${postgres_port} -k ${postgres_socket}" \
  -w start >/dev/null
postgres_started=true
"${pg_bin}/createdb" \
  -h 127.0.0.1 \
  -p "${postgres_port}" \
  -U acsa_owner \
  acsa

git clone --quiet --no-local "${repository_root}" "${checkout_root}"

export DATABASE_DIRECT_URL="postgresql+psycopg://acsa_owner@127.0.0.1:${postgres_port}/acsa"
export DATABASE_URL="postgresql+psycopg://acsa_runtime:local-runtime-password@127.0.0.1:${postgres_port}/acsa"
export TEST_DATABASE_DIRECT_URL="${DATABASE_DIRECT_URL}"
export TEST_DATABASE_URL="${DATABASE_URL}"

cd "${checkout_root}"
pnpm install --frozen-lockfile

cd services/gateway
uv sync --frozen
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head
uv run python -m acsa.adapters.postgres.runtime_role
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q

cd "${checkout_root}"
pnpm lint
pnpm typecheck
pnpm test
pnpm --filter merchantlatch-buyer build
pnpm --filter merchantlatch-merchant build

echo "Clean-clone verification passed for $(git rev-parse HEAD)"
