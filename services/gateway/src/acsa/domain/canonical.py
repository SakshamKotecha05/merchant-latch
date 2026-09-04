"""Canonical JSON helpers shared by signed and persisted domain records."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import orjson


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize a mapping deterministically for hashing, storage, and replay."""
    return orjson.dumps(dict(value), option=orjson.OPT_SORT_KEYS)


def sha256_checksum(value: Mapping[str, Any]) -> str:
    """Return the lowercase SHA-256 checksum of canonical JSON bytes."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
