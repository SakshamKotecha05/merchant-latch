"""Buyer profile validation for the UCP trust boundary."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from cryptography.hazmat.primitives.asymmetric import ec
from http_message_signatures.http_sfv.dictionary import Dictionary
from http_message_signatures.http_sfv.item import Item

from acsa.domain.ucp_checkout import UCP_VERSION
from acsa.security.ucp_signatures import UCPVerificationError, import_public_jwk

_JWK_FIELDS = ("alg", "crv", "kid", "kty", "use", "x", "y")
_PROFILE_MEMBER = re.compile(r"(?:^|,)\s*profile\s*=")
_VERSION_PARAMETER = re.compile(r";\s*version\b")
_VERSION_VALUE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_PROFILE_BYTES = 131_072
_PROFILE_TIMEOUT_SECONDS = 3.0
_CACHE_SECONDS = 300.0
_CACHE_ENTRIES = 256
_INFLIGHT_FETCHES = 64
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class BuyerProfileError(ValueError):
    """A buyer profile failure represented by a safe stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The buyer profile is invalid or unavailable.")


@dataclass(frozen=True, slots=True)
class BuyerIdentity:
    profile_url: str
    origin: str
    key_id: str
    fingerprint: str
    version: str
    public_key: ec.EllipticCurvePublicKey

    @property
    def principal_id(self) -> str:
        material = f"{self.origin}\0{self.key_id}\0{self.fingerprint}".encode()
        return "ucp_" + hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class ProfileHTTPResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class BuyerProfileResolver:
    def __init__(
        self,
        *,
        dns_resolver: Callable[[str], Awaitable[tuple[str, ...]]] | None = None,
        fetch_hop: Callable[..., Awaitable[ProfileHTTPResponse]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._dns_resolver = dns_resolver or resolve_public_dns
        self._fetch_hop: Callable[..., Awaitable[ProfileHTTPResponse]] = fetch_https_profile
        self._monotonic = monotonic or time.monotonic
        if fetch_hop is not None:
            self._fetch_hop = fetch_hop
        self._cache: OrderedDict[tuple[str, str], tuple[float, BuyerIdentity]] = OrderedDict()
        self._inflight: dict[tuple[str, str], asyncio.Task[BuyerIdentity]] = {}

    async def resolve(self, ucp_agent: str, key_id: str) -> BuyerIdentity:
        profile_url = parse_profile_url(ucp_agent)
        cache_key = (profile_url, key_id)
        cached = self._cache.get(cache_key)
        now = self._monotonic()
        if cached is not None and cached[0] > now:
            self._cache.move_to_end(cache_key)
            return cached[1]
        task = self._inflight.get(cache_key)
        if task is None:
            if len(self._inflight) >= _INFLIGHT_FETCHES:
                raise BuyerProfileError("profile_unavailable")
            task = asyncio.create_task(self._resolve_and_cache(profile_url, key_id, cache_key))
            self._inflight[cache_key] = task
            task.add_done_callback(partial(self._clear_inflight, cache_key))
        return await asyncio.shield(task)

    async def _resolve_and_cache(
        self,
        profile_url: str,
        key_id: str,
        cache_key: tuple[str, str],
    ) -> BuyerIdentity:
        try:
            async with asyncio.timeout(_PROFILE_TIMEOUT_SECONDS):
                identity, cache_seconds = await self._resolve_uncached(profile_url, key_id)
        except TimeoutError:
            raise BuyerProfileError("profile_unavailable") from None
        self._cache[cache_key] = (self._monotonic() + cache_seconds, identity)
        self._cache.move_to_end(cache_key)
        while len(self._cache) > _CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return identity

    def _clear_inflight(
        self,
        cache_key: tuple[str, str],
        task: asyncio.Task[BuyerIdentity],
    ) -> None:
        if self._inflight.get(cache_key) is task:
            self._inflight.pop(cache_key)
        if not task.cancelled():
            task.exception()

    async def _resolve_uncached(self, profile_url: str, key_id: str) -> tuple[BuyerIdentity, float]:
        canonical_url, parts = _canonical_profile_url(profile_url)
        hostname = parts.hostname
        if hostname is None:
            raise BuyerProfileError("profile_invalid")
        try:
            addresses = tuple(dict.fromkeys(await self._dns_resolver(hostname)))
        except (OSError, TimeoutError):
            raise BuyerProfileError("profile_unavailable") from None
        if not addresses:
            raise BuyerProfileError("profile_unavailable")
        try:
            parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
        except ValueError:
            raise BuyerProfileError("profile_unavailable") from None
        if not all(is_public_address(address) for address in parsed_addresses):
            raise BuyerProfileError("profile_address_forbidden")
        approved_address = str(
            sorted(parsed_addresses, key=lambda item: (item.version, int(item)))[0]
        )
        try:
            response = await self._fetch_hop(
                url=canonical_url,
                hostname=hostname,
                address=approved_address,
                timeout_seconds=_PROFILE_TIMEOUT_SECONDS,
                max_bytes=_MAX_PROFILE_BYTES,
            )
        except BuyerProfileError:
            raise
        except (
            OSError,
            TimeoutError,
            ssl.SSLError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            raise BuyerProfileError("profile_unavailable") from None
        if len(response.body) > _MAX_PROFILE_BYTES:
            raise BuyerProfileError("profile_too_large")
        if response.status_code in _REDIRECT_STATUSES:
            raise BuyerProfileError("profile_redirect_forbidden")
        if response.status_code != 200:
            raise BuyerProfileError("profile_unavailable")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise BuyerProfileError("profile_invalid")
        cache_seconds = _profile_cache_seconds(response.headers.get("cache-control"))
        try:
            document = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BuyerProfileError("profile_invalid") from None
        return validate_profile_document(canonical_url, document, key_id), cache_seconds


def _profile_cache_seconds(header: str | None) -> float:
    if header is None:
        raise BuyerProfileError("profile_invalid")
    directives = [part.strip().lower() for part in header.split(",")]
    if "public" not in directives or {"private", "no-store", "no-cache"}.intersection(directives):
        raise BuyerProfileError("profile_invalid")
    max_ages = [part.partition("=")[2] for part in directives if part.startswith("max-age=")]
    if len(max_ages) != 1 or not max_ages[0].isdigit() or int(max_ages[0]) < 60:
        raise BuyerProfileError("profile_invalid")
    return min(float(max_ages[0]), _CACHE_SECONDS)


async def resolve_public_dns(hostname: str) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    answers = await loop.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    return tuple(answer[4][0] for answer in answers)


async def fetch_https_profile(
    *,
    url: str,
    hostname: str,
    address: str,
    timeout_seconds: float,
    max_bytes: int,
) -> ProfileHTTPResponse:
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    ssl_context = ssl.create_default_context()
    async with asyncio.timeout(timeout_seconds):
        reader, writer = await asyncio.open_connection(
            address,
            443,
            ssl=ssl_context,
            server_hostname=hostname,
            limit=16_384,
        )
        try:
            request = (
                f"GET {target} HTTP/1.1\r\n"
                f"Host: {hostname}\r\n"
                "Accept: application/json\r\n"
                "Accept-Encoding: identity\r\n"
                "User-Agent: MerchantLatch-UCP/1\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(request)
            await writer.drain()
            header_block = await reader.readuntil(b"\r\n\r\n")
            if len(header_block) > 16_384:
                raise BuyerProfileError("profile_invalid")
            status_code, headers = _parse_http_headers(header_block)
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise BuyerProfileError("profile_invalid")
            body = await _read_http_body(reader, headers, max_bytes)
            return ProfileHTTPResponse(status_code, headers, body)
        finally:
            writer.close()
            await writer.wait_closed()


def _parse_http_headers(header_block: bytes) -> tuple[int, dict[str, str]]:
    try:
        lines = header_block.decode("iso-8859-1").split("\r\n")
        protocol, status, _ = lines[0].split(" ", 2)
        if protocol not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ValueError
        status_code = int(status)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            normalized = name.strip().lower()
            if not separator or not normalized or normalized in headers:
                raise ValueError
            headers[normalized] = value.strip()
    except (UnicodeDecodeError, ValueError):
        raise BuyerProfileError("profile_invalid") from None
    return status_code, headers


async def _read_http_body(
    reader: asyncio.StreamReader,
    headers: dict[str, str],
    max_bytes: int,
) -> bytes:
    transfer_encoding = headers.get("transfer-encoding", "").lower()
    content_length = headers.get("content-length")
    if transfer_encoding:
        if transfer_encoding != "chunked" or content_length is not None:
            raise BuyerProfileError("profile_invalid")
        return await _read_chunked_body(reader, max_bytes)
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError:
            raise BuyerProfileError("profile_invalid") from None
        if length < 0 or length > max_bytes:
            raise BuyerProfileError("profile_too_large")
        return await reader.readexactly(length)
    body = bytearray()
    while True:
        chunk = await reader.read(min(65_536, max_bytes + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > max_bytes:
            raise BuyerProfileError("profile_too_large")


async def _read_chunked_body(reader: asyncio.StreamReader, max_bytes: int) -> bytes:
    body = bytearray()
    while True:
        line = await reader.readline()
        if len(line) > 128 or not line.endswith(b"\r\n"):
            raise BuyerProfileError("profile_invalid")
        try:
            size = int(line[:-2].split(b";", 1)[0], 16)
        except ValueError:
            raise BuyerProfileError("profile_invalid") from None
        if size == 0:
            if await reader.readexactly(2) != b"\r\n":
                raise BuyerProfileError("profile_invalid")
            return bytes(body)
        if size < 0 or len(body) + size > max_bytes:
            raise BuyerProfileError("profile_too_large")
        body.extend(await reader.readexactly(size))
        if await reader.readexactly(2) != b"\r\n":
            raise BuyerProfileError("profile_invalid")


def _canonical_profile_url(value: str) -> tuple[str, SplitResult]:
    try:
        if len(value) > 2048 or any(
            ord(character) <= 32 or ord(character) == 127 for character in value
        ):
            raise ValueError
        parts = urlsplit(value)
        hostname = parts.hostname
        if hostname is None:
            raise ValueError
        ascii_hostname = hostname.encode("idna").decode("ascii").lower().removesuffix(".")
        path = parts.path or "/"
        (path + parts.query).encode("ascii")
        if (
            not ascii_hostname
            or parts.scheme.lower() != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or parts.port not in (None, 443)
            or not path.startswith("/")
        ):
            raise ValueError
        try:
            ipaddress.ip_address(ascii_hostname)
        except ValueError:
            pass
        else:
            raise ValueError
    except (UnicodeError, ValueError):
        raise BuyerProfileError("profile_invalid") from None
    canonical = SplitResult("https", ascii_hostname, path, parts.query, "")
    return urlunsplit(canonical), canonical


def _parse_profile_item(ucp_agent: str) -> Item:
    if (
        len(_PROFILE_MEMBER.findall(ucp_agent)) != 1
        or len(_VERSION_PARAMETER.findall(ucp_agent)) > 1
    ):
        raise BuyerProfileError("profile_invalid")
    try:
        fields = Dictionary()
        fields.parse(ucp_agent.encode("ascii"))
        profile = fields["profile"]
    except (KeyError, UnicodeEncodeError, ValueError):
        raise BuyerProfileError("profile_invalid") from None
    if not isinstance(profile, Item) or not isinstance(profile.value, str):
        raise BuyerProfileError("profile_invalid")
    parameters = list(profile.params)
    if any(parameter != "version" for parameter in parameters):
        raise BuyerProfileError("profile_invalid")
    if parameters:
        version = profile.params["version"]
        if not isinstance(version, str) or _VERSION_VALUE.fullmatch(version) is None:
            raise BuyerProfileError("profile_invalid")
    return profile


def parse_profile_url(ucp_agent: str) -> str:
    profile = _parse_profile_item(ucp_agent)
    canonical, _ = _canonical_profile_url(profile.value)
    return canonical


def parse_ucp_agent_version(ucp_agent: str) -> str | None:
    profile = _parse_profile_item(ucp_agent)
    if "version" not in profile.params:
        return None
    return str(profile.params["version"])


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global


def validate_profile_document(
    profile_url: str,
    document: object,
    key_id: str,
) -> BuyerIdentity:
    canonical_url, parts = _canonical_profile_url(profile_url)
    if not isinstance(key_id, str) or not 1 <= len(key_id) <= 255:
        raise BuyerProfileError("profile_invalid")
    if not isinstance(document, dict):
        raise BuyerProfileError("profile_invalid")
    ucp = document.get("ucp")
    if not isinstance(ucp, dict) or ucp.get("version") != UCP_VERSION:
        raise BuyerProfileError("version_unsupported")
    if not isinstance(ucp.get("services"), dict) or not isinstance(
        ucp.get("payment_handlers"), dict
    ):
        raise BuyerProfileError("profile_invalid")
    keys = document.get("signing_keys")
    if not isinstance(keys, list):
        raise BuyerProfileError("profile_invalid")
    matching = [key for key in keys if isinstance(key, dict) and key.get("kid") == key_id]
    if not matching:
        raise BuyerProfileError("key_not_found")
    if len(matching) != 1:
        raise BuyerProfileError("profile_invalid")
    jwk: dict[str, Any] = matching[0]
    if "d" in jwk:
        raise BuyerProfileError("key_invalid")
    effective_jwk = dict(jwk)
    effective_jwk.setdefault("alg", "ES256")
    effective_jwk.setdefault("use", "sig")
    try:
        public_key = import_public_jwk(effective_jwk)
    except UCPVerificationError as error:
        raise BuyerProfileError(error.code) from None
    canonical_jwk = {field: effective_jwk.get(field) for field in _JWK_FIELDS}
    fingerprint = hashlib.sha256(
        json.dumps(canonical_jwk, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return BuyerIdentity(
        profile_url=canonical_url,
        origin=f"https://{parts.hostname}",
        key_id=key_id,
        fingerprint=fingerprint,
        version=UCP_VERSION,
        public_key=public_key,
    )
