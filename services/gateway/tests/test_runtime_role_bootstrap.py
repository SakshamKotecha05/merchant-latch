from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from acsa.adapters.postgres.runtime_role import RuntimeRoleBootstrapError, bootstrap_runtime_role


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql://runtime_user:runtime-password@pooler.example/acsa",
        "DATABASE_DIRECT_URL": "postgresql://migration_owner:owner-password@direct.example/acsa",
    }


class RecordingCursor:
    def __init__(
        self,
        role_exists: bool,
        parent_roles: tuple[str, ...] = (),
        privileges_valid: bool = True,
        role_state_valid: bool = True,
        fail_statement: str | None = None,
    ) -> None:
        self._role_exists = role_exists
        self._parent_roles = parent_roles
        self._privileges_valid = privileges_valid
        self._role_state_valid = role_state_valid
        self._fail_statement = fail_statement
        self.statements: list[str] = []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: Any) -> None:
        statement = query.as_string(None)
        self.statements.append(statement)
        if self._fail_statement is not None and statement.startswith(self._fail_statement):
            raise RuntimeError("recorded database failure")

    def fetchone(self) -> tuple[int] | None:
        if "AS privileges_valid" in self.statements[-1]:
            return (self._privileges_valid and self._role_state_valid,)
        if "FROM pg_roles WHERE rolname" in self.statements[-1]:
            return (self._role_state_valid,) if self._role_exists else None
        raise AssertionError("unexpected fetchone")

    def fetchall(self) -> list[tuple[str]]:
        assert "FROM pg_auth_members" in self.statements[-1]
        return [(parent_role,) for parent_role in self._parent_roles]


class RecordingConnection:
    def __init__(
        self,
        role_exists: bool,
        parent_roles: tuple[str, ...] = (),
        privileges_valid: bool = True,
        role_state_valid: bool = True,
        fail_statement: str | None = None,
    ) -> None:
        self.cursor_instance = RecordingCursor(
            role_exists,
            parent_roles,
            privileges_valid,
            role_state_valid,
            fail_statement,
        )

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance


@pytest.mark.parametrize(
    ("environment", "expected_message"),
    [
        (
            _environment()
            | {"DATABASE_DIRECT_URL": "postgresql://:owner-password@direct.example/acsa"},
            "DATABASE_DIRECT_URL must include a username",
        ),
        (
            _environment() | {"DATABASE_URL": "postgresql://@pooler.example/acsa"},
            "DATABASE_URL must include a username",
        ),
        (
            _environment() | {"DATABASE_URL": "postgresql://runtime_user@pooler.example/acsa"},
            "DATABASE_URL must include a password",
        ),
        (
            _environment()
            | {"DATABASE_URL": "postgresql://migration_owner:runtime-password@pooler.example/acsa"},
            "distinct database roles",
        ),
        (
            _environment()
            | {"DATABASE_DIRECT_URL": "postgresql://migration_owner:owner-password@direct.example"},
            "DATABASE_DIRECT_URL must include a database name",
        ),
        (
            _environment()
            | {
                "DATABASE_DIRECT_URL": "postgresql://migration_owner:owner-password@direct.example/ledger"
            },
            "same database",
        ),
    ],
)
def test_bootstrap_rejects_invalid_role_credentials_before_connecting(
    environment: Mapping[str, str], expected_message: str
) -> None:
    def must_not_connect(_: str) -> RecordingConnection:
        raise AssertionError("bootstrap opened a connection before validation")

    with pytest.raises(RuntimeRoleBootstrapError, match=expected_message) as caught:
        bootstrap_runtime_role(environment, connect=must_not_connect)

    assert "runtime-password" not in str(caught.value)
    assert "owner-password" not in str(caught.value)
    assert "pooler.example" not in str(caught.value)
    assert "direct.example" not in str(caught.value)


@pytest.mark.parametrize(
    "environment",
    [
        _environment()
        | {
            "DATABASE_URL": (
                "postgresql://runtime_user:runtime-password@pooler.example/acsa?USER=override-user"
            )
        },
        _environment()
        | {
            "DATABASE_DIRECT_URL": (
                "postgresql://migration_owner:owner-password@direct.example/acsa?%75ser=override-owner"
            )
        },
        _environment()
        | {
            "DATABASE_URL": (
                "postgresql://runtime_user:runtime-password@pooler.example/acsa?PaSsWoRd=override-password"
            )
        },
        _environment()
        | {
            "DATABASE_DIRECT_URL": (
                "postgresql://migration_owner:owner-password@direct.example/acsa?dBnAmE=ledger"
            )
        },
        _environment()
        | {
            "DATABASE_URL": (
                "postgresql://runtime_user:runtime-password@pooler.example/acsa?database=ledger"
            )
        },
    ],
)
def test_bootstrap_rejects_query_identity_overrides_before_connecting(
    environment: Mapping[str, str],
) -> None:
    def must_not_connect(_: str) -> RecordingConnection:
        raise AssertionError("bootstrap opened a connection before validation")

    with pytest.raises(RuntimeRoleBootstrapError, match="identity override"):
        bootstrap_runtime_role(environment, connect=must_not_connect)


def test_bootstrap_creates_a_runtime_role_with_only_gateway_privileges(
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = RecordingConnection(role_exists=False)

    bootstrap_runtime_role(_environment(), connect=lambda _: connection)

    statements = connection.cursor_instance.statements
    assert any(
        statement.startswith('CREATE ROLE "runtime_user" WITH LOGIN') for statement in statements
    )
    assert 'GRANT CONNECT ON DATABASE "acsa" TO "runtime_user"' in statements
    assert 'GRANT USAGE ON SCHEMA "public" TO "runtime_user"' in statements
    assert (
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "public" TO "runtime_user"'
        in statements
    )
    assert 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "public" TO "runtime_user"' in statements
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "runtime_user"' in statements
    )
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        'GRANT USAGE, SELECT ON SEQUENCES TO "runtime_user"' in statements
    )
    assert 'REVOKE CREATE ON DATABASE "acsa" FROM "runtime_user"' in statements
    assert 'REVOKE CREATE ON SCHEMA "public" FROM "runtime_user"' in statements
    role_statement = next(
        statement for statement in statements if statement.startswith("CREATE ROLE")
    )
    assert all(
        attribute in role_statement
        for attribute in (
            "NOSUPERUSER",
            "NOCREATEDB",
            "NOCREATEROLE",
            "NOREPLICATION",
            "NOBYPASSRLS",
        )
    )
    assert not any(
        f"GRANT {privilege}" in statement
        for statement in statements
        for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER")
    )
    output = capsys.readouterr().out
    assert "runtime-password" not in output
    assert "owner-password" not in output
    assert "pooler.example" not in output
    assert "direct.example" not in output


def test_bootstrap_reuses_an_existing_restricted_runtime_role_idempotently() -> None:
    connection = RecordingConnection(role_exists=True)

    bootstrap_runtime_role(_environment(), connect=lambda _: connection)

    statements = connection.cursor_instance.statements
    assert not any(statement.startswith("ALTER ROLE") for statement in statements)
    assert not any(statement.startswith('CREATE ROLE "runtime_user"') for statement in statements)


def test_bootstrap_fails_closed_when_runtime_role_attributes_or_memberships_remain() -> None:
    connection = RecordingConnection(role_exists=True, role_state_valid=False)

    with pytest.raises(RuntimeRoleBootstrapError, match="safely restricted"):
        bootstrap_runtime_role(_environment(), connect=lambda _: connection)


def test_bootstrap_post_condition_checks_runtime_role_attributes_and_memberships() -> None:
    connection = RecordingConnection(role_exists=True)

    bootstrap_runtime_role(_environment(), connect=lambda _: connection)

    verification_statement = next(
        statement
        for statement in connection.cursor_instance.statements
        if "AS privileges_valid" in statement
    )
    assert all(
        attribute in verification_statement
        for attribute in (
            "rolcanlogin",
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolinherit",
            "rolreplication",
            "rolbypassrls",
            "rolconnlimit",
            "FROM pg_auth_members",
            "pg_default_acl",
            "defaclnamespace = 0",
            "namespace.nspname = 'public'",
            "aclexplode",
            "acldefault",
            "ARRAY_AGG",
            "('S'::\"char\", 's'::\"char\")",
        )
    )
    assert verification_statement.endswith("::text[] AS privileges_valid")


def test_bootstrap_uses_a_psycopg_direct_connection_url() -> None:
    environment = _environment() | {
        "DATABASE_DIRECT_URL": "postgresql+psycopg://migration_owner:owner-password@direct.example/acsa"
    }
    connection_urls: list[str] = []

    bootstrap_runtime_role(
        environment,
        connect=lambda url: connection_urls.append(url) or RecordingConnection(role_exists=False),
    )

    assert connection_urls == ["postgresql://migration_owner:owner-password@direct.example/acsa"]


@pytest.mark.parametrize("database_url_key", ["DATABASE_URL", "DATABASE_DIRECT_URL"])
def test_bootstrap_rejects_percent_encoded_database_paths_before_connecting(
    database_url_key: str,
) -> None:
    environment = _environment() | {
        database_url_key: _environment()[database_url_key].replace("/acsa", "/acsa%2Dledger")
    }

    def must_not_connect(_: str) -> RecordingConnection:
        raise AssertionError("bootstrap opened a connection before validation")

    with pytest.raises(RuntimeRoleBootstrapError, match="encoded database path") as caught:
        bootstrap_runtime_role(environment, connect=must_not_connect)

    message = str(caught.value)
    assert "runtime-password" not in message
    assert "owner-password" not in message
    assert "pooler.example" not in message
    assert "direct.example" not in message
    assert "acsa%2Dledger" not in message


def test_bootstrap_removes_public_baseline_and_verifies_effective_privileges() -> None:
    connection = RecordingConnection(role_exists=False)

    bootstrap_runtime_role(_environment(), connect=lambda _: connection)

    statements = connection.cursor_instance.statements
    assert 'REVOKE TEMPORARY ON DATABASE "acsa" FROM PUBLIC' in statements
    assert 'REVOKE CREATE ON DATABASE "acsa" FROM PUBLIC' in statements
    assert 'REVOKE CREATE ON SCHEMA "public" FROM PUBLIC' in statements
    assert 'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "public" FROM PUBLIC' in statements
    assert 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "public" FROM PUBLIC' in statements
    assert 'REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA "public" FROM PUBLIC' in statements
    assert (
        'REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA "public" FROM "runtime_user"' in statements
    )
    assert any(
        "ALTER DEFAULT PRIVILEGES" in statement and "FROM PUBLIC" in statement
        for statement in statements
    )
    assert any(
        "ALTER DEFAULT PRIVILEGES" in statement and 'ON ROUTINES FROM "runtime_user"' in statement
        for statement in statements
    )
    verification_statement = next(
        statement for statement in statements if "AS privileges_valid" in statement
    )
    assert "has_function_privilege" in verification_statement
    assert "'EXECUTE'" in verification_statement
    assert "'MAINTAIN'" in verification_statement


def test_bootstrap_uses_global_and_public_schema_default_revokes_before_allowlist_grants() -> None:
    connection = RecordingConnection(role_exists=False)

    bootstrap_runtime_role(_environment(), connect=lambda _: connection)

    statements = connection.cursor_instance.statements
    assert {
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" '
        "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC",
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" '
        'REVOKE ALL PRIVILEGES ON TABLES FROM "runtime_user"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" '
        "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC",
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" '
        'REVOKE ALL PRIVILEGES ON SEQUENCES FROM "runtime_user"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" '
        "REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC",
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" '
        'REVOKE ALL PRIVILEGES ON ROUTINES FROM "runtime_user"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC",
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        'REVOKE ALL PRIVILEGES ON TABLES FROM "runtime_user"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC",
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        'REVOKE ALL PRIVILEGES ON SEQUENCES FROM "runtime_user"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        "REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC",
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        'REVOKE ALL PRIVILEGES ON ROUTINES FROM "runtime_user"',
    }.issubset(statements)
    assert {
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "runtime_user"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "public" '
        'GRANT USAGE, SELECT ON SEQUENCES TO "runtime_user"',
    }.issubset(statements)


def test_bootstrap_fails_closed_when_effective_privileges_remain() -> None:
    connection = RecordingConnection(role_exists=False, privileges_valid=False)

    with pytest.raises(RuntimeRoleBootstrapError, match="effective privileges"):
        bootstrap_runtime_role(_environment(), connect=lambda _: connection)


def test_bootstrap_revokes_every_existing_parent_role_before_granting_access() -> None:
    connection = RecordingConnection(role_exists=True, parent_roles=("readers", "operators"))

    bootstrap_runtime_role(_environment(), connect=lambda _: connection)

    statements = connection.cursor_instance.statements
    membership_query_index = next(
        index for index, statement in enumerate(statements) if "FROM pg_auth_members" in statement
    )
    revoke_indices = [
        statements.index('REVOKE "readers" FROM "runtime_user"'),
        statements.index('REVOKE "operators" FROM "runtime_user"'),
    ]
    grant_index = statements.index('GRANT CONNECT ON DATABASE "acsa" TO "runtime_user"')
    assert membership_query_index < min(revoke_indices) < grant_index


def test_bootstrap_fails_closed_when_parent_role_revocation_fails() -> None:
    connection = RecordingConnection(
        role_exists=True,
        parent_roles=("readers",),
        fail_statement='REVOKE "readers" FROM "runtime_user"',
    )

    with pytest.raises(RuntimeError, match="recorded database failure"):
        bootstrap_runtime_role(_environment(), connect=lambda _: connection)

    assert (
        'GRANT CONNECT ON DATABASE "acsa" TO "runtime_user"'
        not in connection.cursor_instance.statements
    )


def test_bootstrap_locks_the_runtime_role_before_checking_its_existence() -> None:
    connection = RecordingConnection(role_exists=False)

    bootstrap_runtime_role(_environment(), connect=lambda _: connection)

    statements = connection.cursor_instance.statements
    lock_index = next(
        index for index, statement in enumerate(statements) if "pg_advisory_xact_lock" in statement
    )
    role_lookup_index = next(
        index
        for index, statement in enumerate(statements)
        if "FROM pg_roles WHERE rolname" in statement
    )
    assert lock_index < role_lookup_index
