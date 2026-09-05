from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import pytest
from fixture_keys import buyer_private_key

from acsa.security.ucp_signatures import export_public_jwk
from acsa.ucp_profiles import (
    BuyerProfileError,
    BuyerProfileResolver,
    ProfileHTTPResponse,
    _read_http_body,
)


def _document() -> bytes:
    key = buyer_private_key()
    return json.dumps(
        {
            "ucp": {
                "version": "2026-04-08",
                "services": {},
                "payment_handlers": {},
            },
            "signing_keys": [export_public_jwk(key.public_key(), key_id="buyer-p256-2026-01")],
        }
    ).encode()


class FakeClock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value


def _resolver(
    *,
    addresses: tuple[str, ...] = ("8.8.8.8",),
    fetch: Callable[..., Awaitable[ProfileHTTPResponse]] | None = None,
    clock: Callable[[], float] | None = None,
) -> BuyerProfileResolver:
    async def resolve(_: str) -> tuple[str, ...]:
        return addresses

    async def default_fetch(**_: object) -> ProfileHTTPResponse:
        return ProfileHTTPResponse(
            200,
            {
                "content-type": "application/json",
                "cache-control": "public, max-age=300",
            },
            _document(),
        )

    return BuyerProfileResolver(
        dns_resolver=resolve,
        fetch_hop=fetch or default_fetch,
        monotonic=clock,
    )


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("8.8.8.8", "10.0.0.2")])
async def test_rejects_a_hop_if_any_dns_answer_is_not_public(
    addresses: tuple[str, ...],
) -> None:
    resolver = _resolver(addresses=addresses)

    with pytest.raises(BuyerProfileError) as caught:
        await resolver.resolve(
            'profile="https://buyer.example/.well-known/ucp"',
            "buyer-p256-2026-01",
        )

    assert caught.value.code == "profile_address_forbidden"


async def test_fetches_the_original_hostname_through_an_approved_numeric_address() -> None:
    calls: list[dict[str, object]] = []

    async def fetch(**kwargs: object) -> ProfileHTTPResponse:
        calls.append(kwargs)
        return ProfileHTTPResponse(
            200,
            {
                "content-type": "application/json",
                "cache-control": "public, max-age=300",
            },
            _document(),
        )

    resolver = _resolver(addresses=("1.1.1.1",), fetch=fetch)

    identity = await resolver.resolve(
        'profile="https://buyer.example/.well-known/ucp"',
        "buyer-p256-2026-01",
    )

    assert identity.origin == "https://buyer.example"
    assert calls == [
        {
            "url": "https://buyer.example/.well-known/ucp",
            "hostname": "buyer.example",
            "address": "1.1.1.1",
            "timeout_seconds": 3.0,
            "max_bytes": 131072,
        }
    ]


async def test_rejects_redirects_without_fetching_the_target() -> None:
    resolved: list[str] = []
    fetched: list[str] = []

    async def resolve(hostname: str) -> tuple[str, ...]:
        resolved.append(hostname)
        return ("1.1.1.1",)

    async def fetch(**kwargs: object) -> ProfileHTTPResponse:
        fetched.append(str(kwargs["url"]))
        return ProfileHTTPResponse(
            302,
            {"location": "https://profiles.example/buyer.json"},
            b"",
        )

    resolver = BuyerProfileResolver(dns_resolver=resolve, fetch_hop=fetch)

    with pytest.raises(BuyerProfileError) as caught:
        await resolver.resolve(
            'profile="https://buyer.example/.well-known/ucp"',
            "buyer-p256-2026-01",
        )

    assert caught.value.code == "profile_redirect_forbidden"
    assert resolved == ["buyer.example"]
    assert fetched == ["https://buyer.example/.well-known/ucp"]


@pytest.mark.parametrize(
    "response, code",
    [
        (ProfileHTTPResponse(200, {"content-type": "text/html"}, _document()), "profile_invalid"),
        (
            ProfileHTTPResponse(
                200,
                {"content-type": "application/json"},
                b"x" * 131073,
            ),
            "profile_too_large",
        ),
        (
            ProfileHTTPResponse(503, {"content-type": "application/json"}, b"{}"),
            "profile_unavailable",
        ),
        (
            ProfileHTTPResponse(
                200,
                {"content-type": "application/json", "cache-control": "private, max-age=300"},
                _document(),
            ),
            "profile_invalid",
        ),
        (
            ProfileHTTPResponse(
                200,
                {"content-type": "application/json", "cache-control": "public, max-age=59"},
                _document(),
            ),
            "profile_invalid",
        ),
    ],
)
async def test_rejects_invalid_profile_responses(
    response: ProfileHTTPResponse,
    code: str,
) -> None:
    async def fetch(**_: object) -> ProfileHTTPResponse:
        return response

    with pytest.raises(BuyerProfileError) as caught:
        await _resolver(fetch=fetch).resolve(
            'profile="https://buyer.example/.well-known/ucp"',
            "buyer-p256-2026-01",
        )

    assert caught.value.code == code


async def test_coalesces_concurrent_fetches_and_caches_success_for_five_minutes() -> None:
    clock = FakeClock()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(**_: object) -> ProfileHTTPResponse:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ProfileHTTPResponse(
            200,
            {
                "content-type": "application/json",
                "cache-control": "public, max-age=300",
            },
            _document(),
        )

    resolver = _resolver(fetch=fetch, clock=clock)
    first = asyncio.create_task(
        resolver.resolve(
            'profile="https://buyer.example/.well-known/ucp"',
            "buyer-p256-2026-01",
        )
    )
    await started.wait()
    second = asyncio.create_task(
        resolver.resolve(
            'profile="https://buyer.example/.well-known/ucp"',
            "buyer-p256-2026-01",
        )
    )
    release.set()
    await asyncio.gather(first, second)
    await resolver.resolve(
        'profile="https://buyer.example/.well-known/ucp"',
        "buyer-p256-2026-01",
    )
    assert calls == 1

    clock.value += 301
    await resolver.resolve(
        'profile="https://buyer.example/.well-known/ucp"',
        "buyer-p256-2026-01",
    )
    assert calls == 2


async def test_profile_cache_has_a_fixed_256_entry_bound() -> None:
    calls = 0

    async def fetch(**_: object) -> ProfileHTTPResponse:
        nonlocal calls
        calls += 1
        return ProfileHTTPResponse(
            200,
            {
                "content-type": "application/json",
                "cache-control": "public, max-age=300",
            },
            _document(),
        )

    resolver = _resolver(fetch=fetch)
    for index in range(257):
        await resolver.resolve(
            f'profile="https://buyer-{index}.example/.well-known/ucp"',
            "buyer-p256-2026-01",
        )
    await resolver.resolve(
        'profile="https://buyer-0.example/.well-known/ucp"',
        "buyer-p256-2026-01",
    )

    assert calls == 258


async def test_failed_profile_fetches_do_not_accumulate_coordination_state() -> None:
    async def fetch(**_: object) -> ProfileHTTPResponse:
        raise BuyerProfileError("profile_invalid")

    resolver = _resolver(fetch=fetch)
    for index in range(300):
        with pytest.raises(BuyerProfileError):
            await resolver.resolve(
                f'profile="https://invalid-{index}.example/.well-known/ucp"',
                "buyer-p256-2026-01",
            )

    assert resolver._inflight == {}  # noqa: SLF001


async def test_rejects_new_unique_fetches_when_the_inflight_bound_is_full() -> None:
    started_count = 0
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(**_: object) -> ProfileHTTPResponse:
        nonlocal started_count
        started_count += 1
        if started_count == 64:
            all_started.set()
        await release.wait()
        return ProfileHTTPResponse(
            200,
            {
                "content-type": "application/json",
                "cache-control": "public, max-age=300",
            },
            _document(),
        )

    resolver = _resolver(fetch=fetch)
    pending = [
        asyncio.create_task(
            resolver.resolve(
                f'profile="https://buyer-{index}.example/.well-known/ucp"',
                "buyer-p256-2026-01",
            )
        )
        for index in range(64)
    ]
    await all_started.wait()

    with pytest.raises(BuyerProfileError) as caught:
        await resolver.resolve(
            'profile="https://overflow.example/.well-known/ucp"',
            "buyer-p256-2026-01",
        )

    assert caught.value.code == "profile_unavailable"
    release.set()
    await asyncio.gather(*pending)


async def test_maps_oversized_header_reader_failures_to_a_safe_profile_error() -> None:
    async def fetch(**_: object) -> ProfileHTTPResponse:
        raise asyncio.LimitOverrunError("profile headers exceed the reader limit", 16_385)

    with pytest.raises(BuyerProfileError) as caught:
        await _resolver(fetch=fetch).resolve(
            'profile="https://buyer.example/.well-known/ucp"',
            "buyer-p256-2026-01",
        )

    assert caught.value.code == "profile_unavailable"


async def test_connection_close_body_reader_collects_segmented_data_until_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"ucp":')
    pending = asyncio.create_task(_read_http_body(reader, {}, 128))
    await asyncio.sleep(0)
    reader.feed_data(b'{"version":"2026-04-08"}}')
    reader.feed_eof()

    body = await pending

    assert body == b'{"ucp":{"version":"2026-04-08"}}'
