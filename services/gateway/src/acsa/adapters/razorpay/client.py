from __future__ import annotations

from collections.abc import Mapping

import httpx

from acsa.domain.receipts import ProviderOrderCandidate


class RazorpayProviderError(RuntimeError):
    def __init__(self, *, status_code: int | None, operation: str) -> None:
        self.status_code = status_code
        self.operation = operation
        status = str(status_code) if status_code is not None else "unknown"
        super().__init__(f"Razorpay {operation} failed with status {status}")


class RazorpayClient:
    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        http_client: httpx.AsyncClient,
        base_url: str = "https://api.razorpay.com/v1",
    ) -> None:
        self._auth = httpx.BasicAuth(key_id, key_secret)
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")

    async def create_order(
        self,
        *,
        amount_minor: int,
        currency: str,
        receipt: str,
        notes: Mapping[str, str],
    ) -> ProviderOrderCandidate:
        response = await self._http_client.post(
            f"{self._base_url}/orders",
            auth=self._auth,
            json={
                "amount": amount_minor,
                "currency": currency,
                "receipt": receipt,
                "notes": dict(notes),
                "partial_payment": False,
            },
        )
        self._raise_for_provider_error(response, operation="create_order")
        return _parse_order(response.json(), operation="create_order")

    async def fetch_orders_by_receipt(self, receipt: str) -> list[ProviderOrderCandidate]:
        response = await self._http_client.get(
            f"{self._base_url}/orders",
            auth=self._auth,
            params={"receipt": receipt, "count": 100},
        )
        self._raise_for_provider_error(response, operation="fetch_orders")
        body = response.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise RazorpayProviderError(status_code=response.status_code, operation="fetch_orders")
        return [_parse_order(item, operation="fetch_orders") for item in items]

    @staticmethod
    def _raise_for_provider_error(response: httpx.Response, *, operation: str) -> None:
        if response.is_error:
            raise RazorpayProviderError(status_code=response.status_code, operation=operation)


def _parse_order(payload: object, *, operation: str) -> ProviderOrderCandidate:
    if not isinstance(payload, dict):
        raise RazorpayProviderError(status_code=None, operation=operation)
    order_id = payload.get("id")
    receipt = payload.get("receipt")
    amount = payload.get("amount")
    currency = payload.get("currency")
    raw_notes = payload.get("notes", {})
    if (
        not isinstance(order_id, str)
        or not isinstance(receipt, str)
        or not isinstance(amount, int)
        or isinstance(amount, bool)
        or not isinstance(currency, str)
        or not isinstance(raw_notes, dict)
    ):
        raise RazorpayProviderError(status_code=None, operation=operation)
    notes = {str(key): str(value) for key, value in raw_notes.items()}
    return ProviderOrderCandidate(
        order_id=order_id,
        receipt=receipt,
        amount_minor=amount,
        currency=currency,
        notes=notes,
    )
