from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import psycopg
from psycopg import sql
from pydantic import PostgresDsn, ValidationError

from acsa.database_urls import has_identity_query_override, has_percent_encoded_database_path

RUNTIME_CONNECTION_LIMIT = 10


class RuntimeRoleBootstrapError(RuntimeError):
    """A secret-safe runtime database role bootstrap failure."""


@dataclass(frozen=True, slots=True)
class RuntimeRoleSettings:
    direct_url: str
    database_name: str
    owner_username: str
    runtime_username: str
    runtime_password: str


def bootstrap_runtime_role(
    environment: Mapping[str, str],
    *,
    connect: Callable[[str], psycopg.Connection[Any]] = psycopg.connect,
) -> None:
    settings = load_runtime_role_settings(environment)
    with connect(settings.direct_url) as connection, connection.cursor() as cursor:
        cursor.execute(_advisory_lock_statement(settings))
        cursor.execute(
            sql.SQL("SELECT 1 FROM pg_roles WHERE rolname = {}").format(
                sql.Literal(settings.runtime_username)
            )
        )
        if cursor.fetchone() is None:
            cursor.execute(_create_role_statement(settings))
        else:
            cursor.execute(_alter_role_statement(settings))
        _revoke_parent_role_memberships(cursor, settings)
        for statement in _privilege_statements(settings):
            cursor.execute(statement)
        cursor.execute(_effective_privileges_statement(settings))
        result = cursor.fetchone()
        if result != (True,):
            raise RuntimeRoleBootstrapError(
                "Runtime role effective privileges could not be verified"
            )


def load_runtime_role_settings(environment: Mapping[str, str]) -> RuntimeRoleSettings:
    direct_url = _parse_database_url("DATABASE_DIRECT_URL", environment)
    runtime_url = _parse_database_url("DATABASE_URL", environment)
    _reject_identity_query_overrides("DATABASE_DIRECT_URL", direct_url)
    _reject_identity_query_overrides("DATABASE_URL", runtime_url)
    _reject_percent_encoded_database_path("DATABASE_DIRECT_URL", direct_url)
    _reject_percent_encoded_database_path("DATABASE_URL", runtime_url)
    owner_username, _, database_name = _url_parts("DATABASE_DIRECT_URL", direct_url)
    runtime_username, runtime_password, runtime_database_name = _url_parts(
        "DATABASE_URL", runtime_url
    )
    if runtime_password is None:
        raise RuntimeRoleBootstrapError("DATABASE_URL must include a password")
    if owner_username == runtime_username:
        raise RuntimeRoleBootstrapError(
            "DATABASE_URL and DATABASE_DIRECT_URL must use distinct database roles"
        )
    if database_name != runtime_database_name:
        raise RuntimeRoleBootstrapError(
            "DATABASE_URL and DATABASE_DIRECT_URL must identify the same database"
        )
    return RuntimeRoleSettings(
        direct_url=_psycopg_url(direct_url),
        database_name=database_name,
        owner_username=owner_username,
        runtime_username=runtime_username,
        runtime_password=runtime_password,
    )


def main() -> None:
    try:
        bootstrap_runtime_role(os.environ)
    except RuntimeRoleBootstrapError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("Runtime role bootstrap failed") from None
    print("Runtime database role provisioned")


def _parse_database_url(name: str, environment: Mapping[str, str]) -> PostgresDsn:
    raw_url = environment.get(name)
    if not raw_url:
        raise RuntimeRoleBootstrapError(f"{name} is required")
    try:
        return PostgresDsn(raw_url)
    except ValidationError:
        raise RuntimeRoleBootstrapError(f"{name} must be a PostgreSQL URL") from None


def _url_parts(name: str, url: PostgresDsn) -> tuple[str, str | None, str]:
    hosts = url.hosts()
    username = _decode_url_component(hosts[0]["username"])
    if username is None:
        raise RuntimeRoleBootstrapError(f"{name} must include a username")
    path = url.path
    if path is None:
        raise RuntimeRoleBootstrapError(f"{name} must include a database name")
    database_name = _decode_url_component(path.lstrip("/"))
    if not database_name:
        raise RuntimeRoleBootstrapError(f"{name} must include a database name")
    return username, _decode_url_component(hosts[0]["password"]), database_name


def _decode_url_component(value: str | None) -> str | None:
    return unquote(value) if value is not None else None


def _psycopg_url(url: PostgresDsn) -> str:
    return url.unicode_string().replace("postgresql+psycopg://", "postgresql://", 1)


def _reject_identity_query_overrides(name: str, url: PostgresDsn) -> None:
    if has_identity_query_override(url):
        raise RuntimeRoleBootstrapError(f"{name} must not include identity override parameters")


def _reject_percent_encoded_database_path(name: str, url: PostgresDsn) -> None:
    if has_percent_encoded_database_path(url):
        raise RuntimeRoleBootstrapError(
            f"{name} must not include percent-encoded database path components"
        )


def _advisory_lock_statement(settings: RuntimeRoleSettings) -> sql.Composed:
    return sql.SQL("SELECT pg_advisory_xact_lock(hashtextextended({}, {}))").format(
        sql.Literal(settings.runtime_username),
        sql.Literal(0),
    )


def _revoke_parent_role_memberships(
    cursor: psycopg.Cursor[Any], settings: RuntimeRoleSettings
) -> None:
    cursor.execute(
        sql.SQL(
            "SELECT parent.rolname "
            "FROM pg_auth_members AS membership "
            "JOIN pg_roles AS parent ON parent.oid = membership.roleid "
            "JOIN pg_roles AS member ON member.oid = membership.member "
            "WHERE member.rolname = {}"
        ).format(sql.Literal(settings.runtime_username))
    )
    for row in cursor.fetchall():
        parent_role = row[0]
        if not isinstance(parent_role, str):
            raise RuntimeRoleBootstrapError("Runtime role memberships could not be verified")
        cursor.execute(
            sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(parent_role),
                sql.Identifier(settings.runtime_username),
            )
        )


def _create_role_statement(settings: RuntimeRoleSettings) -> sql.Composed:
    return sql.SQL(
        "CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {}"
    ).format(
        sql.Identifier(settings.runtime_username),
        sql.Literal(settings.runtime_password),
        sql.Literal(RUNTIME_CONNECTION_LIMIT),
    )


def _alter_role_statement(settings: RuntimeRoleSettings) -> sql.Composed:
    return sql.SQL(
        "ALTER ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {}"
    ).format(
        sql.Identifier(settings.runtime_username),
        sql.Literal(settings.runtime_password),
        sql.Literal(RUNTIME_CONNECTION_LIMIT),
    )


def _privilege_statements(settings: RuntimeRoleSettings) -> list[sql.Composed]:
    database = sql.Identifier(settings.database_name)
    schema = sql.Identifier("public")
    owner = sql.Identifier(settings.owner_username)
    runtime = sql.Identifier(settings.runtime_username)
    return [
        sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(database),
        sql.SQL("REVOKE CREATE ON DATABASE {} FROM PUBLIC").format(database),
        sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, runtime),
        sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(database, runtime),
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, runtime),
        sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(schema),
        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(schema, runtime),
        sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(schema, runtime),
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, runtime),
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(schema),
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}").format(schema, runtime),
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
            schema, runtime
        ),
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC").format(schema),
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(
            schema, runtime
        ),
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(schema, runtime),
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA {} FROM PUBLIC").format(schema),
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA {} FROM {}").format(
            schema, runtime
        ),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC"
        ).format(owner),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL PRIVILEGES ON TABLES FROM {}"
        ).format(owner, runtime),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC"
        ).format(owner, schema),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "REVOKE ALL PRIVILEGES ON TABLES FROM {}"
        ).format(owner, schema, runtime),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(owner, schema, runtime),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC"
        ).format(owner),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}"
        ).format(owner, runtime),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC"
        ).format(owner, schema),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}"
        ).format(owner, schema, runtime),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "GRANT USAGE, SELECT ON SEQUENCES TO {}"
        ).format(owner, schema, runtime),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC"
        ).format(owner),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL PRIVILEGES ON ROUTINES FROM {}"
        ).format(owner, runtime),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC"
        ).format(owner, schema),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
            "REVOKE ALL PRIVILEGES ON ROUTINES FROM {}"
        ).format(owner, schema, runtime),
    ]


def _effective_privileges_statement(settings: RuntimeRoleSettings) -> sql.Composed:
    runtime = sql.Literal(settings.runtime_username)
    owner = sql.Literal(settings.owner_username)
    database = sql.Literal(settings.database_name)
    schema = sql.Literal("public")
    return sql.SQL(
        "SELECT "
        "has_database_privilege({runtime}, {database}, 'CONNECT') "
        "AND NOT has_database_privilege({runtime}, {database}, 'CREATE') "
        "AND NOT has_database_privilege({runtime}, {database}, 'TEMPORARY') "
        "AND has_schema_privilege({runtime}, {schema}, 'USAGE') "
        "AND NOT has_schema_privilege({runtime}, {schema}, 'CREATE') "
        "AND NOT EXISTS ("
        "SELECT 1 FROM pg_class AS relation "
        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
        "WHERE namespace.nspname = {schema} "
        "AND relation.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "AND ("
        "NOT has_table_privilege({runtime}, relation.oid, 'SELECT') "
        "OR NOT has_table_privilege({runtime}, relation.oid, 'INSERT') "
        "OR NOT has_table_privilege({runtime}, relation.oid, 'UPDATE') "
        "OR NOT has_table_privilege({runtime}, relation.oid, 'DELETE') "
        "OR has_table_privilege({runtime}, relation.oid, 'TRUNCATE') "
        "OR has_table_privilege({runtime}, relation.oid, 'REFERENCES') "
        "OR has_table_privilege({runtime}, relation.oid, 'TRIGGER') "
        "OR has_table_privilege({runtime}, relation.oid, 'MAINTAIN')"
        ")"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM pg_class AS sequence "
        "JOIN pg_namespace AS namespace ON namespace.oid = sequence.relnamespace "
        "WHERE namespace.nspname = {schema} "
        "AND sequence.relkind = 'S' "
        "AND ("
        "NOT has_sequence_privilege({runtime}, sequence.oid, 'USAGE') "
        "OR NOT has_sequence_privilege({runtime}, sequence.oid, 'SELECT') "
        "OR has_sequence_privilege({runtime}, sequence.oid, 'UPDATE')"
        ")"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM pg_proc AS routine "
        "JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace "
        "WHERE namespace.nspname = {schema} "
        "AND has_function_privilege({runtime}, routine.oid, 'EXECUTE')"
        ") "
        "AND EXISTS ("
        "SELECT 1 FROM pg_roles AS runtime_role "
        "WHERE runtime_role.rolname = {runtime} "
        "AND runtime_role.rolcanlogin "
        "AND NOT runtime_role.rolsuper "
        "AND NOT runtime_role.rolcreatedb "
        "AND NOT runtime_role.rolcreaterole "
        "AND NOT runtime_role.rolinherit "
        "AND NOT runtime_role.rolreplication "
        "AND NOT runtime_role.rolbypassrls "
        "AND runtime_role.rolconnlimit = {connection_limit}"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM pg_auth_members AS membership "
        "JOIN pg_roles AS member ON member.oid = membership.member "
        "WHERE member.rolname = {runtime}"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 "
        "FROM pg_roles AS default_owner "
        "CROSS JOIN (VALUES "
        "('r'::\"char\", 'r'::\"char\"), "
        "('S'::\"char\", 's'::\"char\"), "
        "('f'::\"char\", 'f'::\"char\")"
        ") AS object_type(kind, acldefault_kind) "
        "LEFT JOIN pg_default_acl AS default_acl "
        "ON default_acl.defaclrole = default_owner.oid "
        "AND default_acl.defaclnamespace = 0 "
        "AND default_acl.defaclobjtype = object_type.kind "
        "CROSS JOIN LATERAL aclexplode("
        "COALESCE(default_acl.defaclacl, "
        "acldefault(object_type.acldefault_kind, default_owner.oid))"
        ") AS default_privilege(grantor, grantee, privilege_type, is_grantable) "
        "WHERE default_owner.rolname = {owner} "
        "AND default_privilege.grantee IN ("
        "0, (SELECT oid FROM pg_roles WHERE rolname = {runtime})"
        ")"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 "
        "FROM pg_default_acl AS default_acl "
        "JOIN pg_roles AS default_owner ON default_owner.oid = default_acl.defaclrole "
        "JOIN pg_namespace AS namespace ON namespace.oid = default_acl.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) "
        "AS default_privilege(grantor, grantee, privilege_type, is_grantable) "
        "WHERE default_owner.rolname = {owner} "
        "AND namespace.nspname = {schema} "
        "AND default_privilege.grantee = 0"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 "
        "FROM pg_default_acl AS default_acl "
        "JOIN pg_roles AS default_owner ON default_owner.oid = default_acl.defaclrole "
        "JOIN pg_namespace AS namespace ON namespace.oid = default_acl.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) "
        "AS default_privilege(grantor, grantee, privilege_type, is_grantable) "
        "WHERE default_owner.rolname = {owner} "
        "AND namespace.nspname = {schema} "
        "AND default_acl.defaclobjtype = 'f' "
        "AND default_privilege.grantee = (SELECT oid FROM pg_roles WHERE rolname = {runtime})"
        ") "
        "AND ("
        "SELECT ARRAY_AGG(default_privilege.privilege_type "
        "ORDER BY default_privilege.privilege_type) "
        "FROM pg_default_acl AS default_acl "
        "JOIN pg_roles AS default_owner ON default_owner.oid = default_acl.defaclrole "
        "JOIN pg_namespace AS namespace ON namespace.oid = default_acl.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) "
        "AS default_privilege(grantor, grantee, privilege_type, is_grantable) "
        "WHERE default_owner.rolname = {owner} "
        "AND namespace.nspname = {schema} "
        "AND default_acl.defaclobjtype = 'r' "
        "AND default_privilege.grantee = (SELECT oid FROM pg_roles WHERE rolname = {runtime})"
        ") = ARRAY['DELETE', 'INSERT', 'SELECT', 'UPDATE']::text[] "
        "AND ("
        "SELECT ARRAY_AGG(default_privilege.privilege_type "
        "ORDER BY default_privilege.privilege_type) "
        "FROM pg_default_acl AS default_acl "
        "JOIN pg_roles AS default_owner ON default_owner.oid = default_acl.defaclrole "
        "JOIN pg_namespace AS namespace ON namespace.oid = default_acl.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) "
        "AS default_privilege(grantor, grantee, privilege_type, is_grantable) "
        "WHERE default_owner.rolname = {owner} "
        "AND namespace.nspname = {schema} "
        "AND default_acl.defaclobjtype = 'S' "
        "AND default_privilege.grantee = (SELECT oid FROM pg_roles WHERE rolname = {runtime})"
        ") = ARRAY['SELECT', 'USAGE']::text[] "
        "AS privileges_valid"
    ).format(
        runtime=runtime,
        owner=owner,
        database=database,
        schema=schema,
        connection_limit=sql.Literal(RUNTIME_CONNECTION_LIMIT),
    )


if __name__ == "__main__":
    main()
