from __future__ import annotations

from urllib.parse import parse_qsl, unquote

from pydantic import PostgresDsn

IDENTITY_QUERY_KEYS = frozenset({"user", "username", "password", "dbname", "database"})


def has_identity_query_override(url: PostgresDsn) -> bool:
    return any(
        name.casefold() in IDENTITY_QUERY_KEYS
        for name, _ in parse_qsl(url.query, keep_blank_values=True)
    )


def has_percent_encoded_database_path(url: PostgresDsn) -> bool:
    path = url.path
    return path is not None and unquote(path) != path
