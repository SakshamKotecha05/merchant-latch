"""Canonical, secret-safe audit evidence persistence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import orjson
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from acsa.adapters.postgres.models import AuditEvent
from acsa.domain.canonical import canonical_json_bytes

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "secret",
    "signing_key",
    "signature",
    "token",
)


async def append_audit_event(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    evidence_source: str,
) -> None:
    sequence = await session.scalar(
        select(func.coalesce(func.max(AuditEvent.sequence), 0)).where(
            AuditEvent.aggregate_type == aggregate_type,
            AuditEvent.aggregate_id == aggregate_id,
        )
    )
    safe_payload = _canonical_safe_payload(payload)
    session.add(
        AuditEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            sequence=int(sequence or 0) + 1,
            event_type=event_type,
            payload=safe_payload,
            evidence_source=evidence_source,
        )
    )


def _canonical_safe_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    redacted = _redact_mapping(payload)
    canonical = orjson.loads(canonical_json_bytes(redacted))
    if not isinstance(canonical, dict):
        raise ValueError("Audit payload must be a JSON object")
    return canonical


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _REDACTED if _is_sensitive(str(key)) else _redact_value(item)
        for key, item in value.items()
    }


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _is_sensitive(key: str) -> bool:
    normalized = key.casefold()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)
