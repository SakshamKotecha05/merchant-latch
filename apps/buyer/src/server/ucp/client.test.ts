import { createHash, generateKeyPairSync, sign } from "node:crypto";

import { describe, expect, it } from "vitest";

import type { BuyerConfig } from "../config";
import { canonicalJson } from "./canonical";
import { UcpCheckoutClient, UcpCheckoutError } from "./client";
import { exportPublicJwk, importPublicJwk } from "./keys";

const now = new Date("2026-09-04T12:00:00Z");
const epoch = Math.floor(now.getTime() / 1_000);
const buyerKeys = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
const merchantKeys = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
const merchantKeyId = "merchant-key-1";

const responseBody = (bytes: Uint8Array): ArrayBuffer =>
  bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;

const config: BuyerConfig = {
  geminiApiKey: "gemini-secret-value",
  geminiModel: "gemini-3.8-flash",
  buyerPrivateKeyPem: buyerKeys.privateKey.export({ format: "pem", type: "pkcs8" }).toString(),
  buyerKeyId: "buyer-key-1",
  sessionSecret: "s".repeat(32),
  publicBuyerUrl: new URL("https://buyer.example/"),
  publicGatewayUrl: new URL("https://gateway.example/"),
  publicMerchantUrl: new URL("https://merchant.example/"),
};

const merchantProfile = () => ({
  ucp: {
    version: "2026-04-08",
    services: {
      "dev.ucp.shopping": [
        {
          version: "2026-04-08",
          transport: "rest",
          endpoint: "https://gateway.example/ucp/shopping",
        },
      ],
    },
    capabilities: {
      "dev.ucp.shopping.checkout": [{ version: "2026-04-08" }],
    },
  },
  signing_keys: [exportPublicJwk(merchantKeys.privateKey, merchantKeyId)],
});

const checkout = (change: Record<string, unknown> = {}) => ({
  ucp: {
    version: "2026-04-08",
    capabilities: { "dev.ucp.shopping.checkout": [{ version: "2026-04-08" }] },
    payment_handlers: {},
  },
  id: "chk_1",
  status: "requires_escalation",
  currency: "INR",
  line_items: [
    {
      id: "var_1",
      item: { id: "var_1", title: "Stride Runner", price: 249_900 },
      quantity: 1,
      totals: [
        { type: "subtotal", amount: 249_900 },
        { type: "total", amount: 249_900 },
      ],
    },
  ],
  totals: [
    { type: "subtotal", amount: 249_900 },
    { type: "total", amount: 249_900 },
  ],
  links: [],
  messages: [
    {
      type: "error",
      code: "merchant_review_required",
      content: "Continue with the merchant.",
      severity: "requires_buyer_review",
    },
  ],
  continue_url: "https://merchant.example/checkout/chk_1",
  ...change,
});

const signedResponse = (
  value: unknown,
  options: {
    keyId?: string;
    privateKey?: typeof merchantKeys.privateKey;
    created?: number;
    expires?: number;
    status?: number;
  } = {},
): Response => {
  const body = canonicalJson(value);
  const status = options.status ?? 201;
  const digest = `sha-256=:${createHash("sha256").update(body).digest("base64")}:`;
  const signatureInput = `sig1=("@status" "content-digest" "content-type");created=${options.created ?? epoch};keyid="${options.keyId ?? merchantKeyId}";expires=${options.expires ?? epoch + 300}`;
  const base = [
    `"@status": ${status}`,
    `"content-digest": ${digest}`,
    '"content-type": application/json',
    `"@signature-params": ${signatureInput.slice(5)}`,
  ].join("\n");
  const signature = sign("sha256", Buffer.from(base), {
    key: options.privateKey ?? merchantKeys.privateKey,
    dsaEncoding: "ieee-p1363",
  });
  return new Response(responseBody(body), {
    status,
    headers: {
      "content-type": "application/json",
      "content-digest": digest,
      "signature-input": signatureInput,
      signature: `sig1=:${signature.toString("base64")}:`,
    },
  });
};

const profileResponse = (): Response =>
  new Response(JSON.stringify(merchantProfile()), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

const input = {
  requestId: "123e4567-e89b-42d3-a456-426614174000",
  variantId: "var_1",
  quantity: 1,
  budgetMinor: 300_000,
};

describe("UcpCheckoutClient", () => {
  it("sends one canonical signed request and returns verified escalation data", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const client = new UcpCheckoutClient(config, {
      now: () => now,
      nonce: () => "123e4567-e89b-42d3-a456-426614174001",
      fetcher: async (requestInput, init) => {
        requests.push({ url: String(requestInput), init });
        return requests.length === 1 ? profileResponse() : signedResponse(checkout());
      },
    });

    const result = await client.createCheckout(input, new AbortController().signal);

    expect(requests).toHaveLength(2);
    const request = requests[1];
    expect(request?.url).toBe("https://gateway.example/ucp/shopping/checkout-sessions");
    expect(new TextDecoder().decode(request?.init?.body as Uint8Array)).toBe(
      '{"budget_minor":300000,"line_items":[{"item":{"id":"var_1"},"quantity":1}]}',
    );
    const headers = new Headers(request?.init?.headers);
    expect(headers.get("ucp-agent")).toBe('profile="https://buyer.example/.well-known/ucp"');
    expect(headers.get("idempotency-key")).toBe(input.requestId);
    expect(headers.get("signature-input")).toContain(
      ';created=1788523200;keyid="buyer-key-1";expires=1788523500;nonce="123e4567-e89b-42d3-a456-426614174001"',
    );
    expect(result.outcome).toBe("requires_escalation");
    expect(result.continueUrl).toBe("https://merchant.example/checkout/chk_1");
    expect(result.data).toMatchObject({ id: "chk_1", currency: "INR" });
  });

  it("uses a fresh nonce but keeps confirmation-bound idempotency on another invocation", async () => {
    let calls = 0;
    const nonces = [
      "123e4567-e89b-42d3-a456-426614174001",
      "123e4567-e89b-42d3-a456-426614174002",
    ];
    const signatures: string[] = [];
    const client = new UcpCheckoutClient(config, {
      now: () => now,
      nonce: () => nonces.shift()!,
      fetcher: async (_request, init) => {
        calls += 1;
        if (calls % 2 === 1) return profileResponse();
        const headers = new Headers(init?.headers);
        signatures.push(headers.get("signature-input") ?? "");
        expect(headers.get("idempotency-key")).toBe(input.requestId);
        return signedResponse(checkout());
      },
    });

    await client.createCheckout(input, new AbortController().signal);
    await client.createCheckout(input, new AbortController().signal);

    expect(signatures[0]).toContain('nonce="123e4567-e89b-42d3-a456-426614174001"');
    expect(signatures[1]).toContain('nonce="123e4567-e89b-42d3-a456-426614174002"');
  });

  it("does not retry when dispatch has an uncertain result", async () => {
    let calls = 0;
    const client = new UcpCheckoutClient(config, {
      now: () => now,
      fetcher: async () => {
        calls += 1;
        if (calls === 1) return profileResponse();
        throw new Error("socket closed after write");
      },
    });

    await expect(
      client.createCheckout(input, new AbortController().signal),
    ).rejects.toMatchObject({ code: "checkout_outcome_unknown" });
    expect(calls).toBe(2);
  });

  it.each([
    ["invalid signed schema", signedResponse({ id: "not-a-checkout" }), "merchant_response_invalid"],
    [
      "wrong signing key",
      signedResponse(checkout(), { privateKey: buyerKeys.privateKey }),
      "merchant_response_invalid",
    ],
    [
      "wrong key ID",
      signedResponse(checkout(), { keyId: "rotated-key" }),
      "merchant_response_invalid",
    ],
    [
      "stale signature",
      signedResponse(checkout(), { created: epoch - 601, expires: epoch - 301 }),
      "merchant_response_invalid",
    ],
    [
      "cross-origin continuation",
      signedResponse(checkout({ continue_url: "https://evil.example/checkout/chk_1" })),
      "merchant_response_invalid",
    ],
    [
      "gateway-origin continuation",
      signedResponse(checkout({ continue_url: "https://gateway.example/checkout/chk_1" })),
      "merchant_response_invalid",
    ],
    [
      "non-HTTPS continuation",
      signedResponse(checkout({ continue_url: "http://merchant.example/checkout/chk_1" })),
      "merchant_response_invalid",
    ],
    [
      "oversized body",
      new Response("{}", {
        status: 201,
        headers: { "content-type": "application/json", "content-length": "524289" },
      }),
      "merchant_response_invalid",
    ],
  ])("rejects %s", async (_label, checkoutResponse, code) => {
    let calls = 0;
    const client = new UcpCheckoutClient(config, {
      now: () => now,
      fetcher: async () => (++calls === 1 ? profileResponse() : checkoutResponse),
    });

    await expect(
      client.createCheckout(input, new AbortController().signal),
    ).rejects.toMatchObject({ code });
  });

  it("rejects a changed body whose signed digest no longer matches", async () => {
    const original = signedResponse(checkout());
    const headers = new Headers(original.headers);
    let calls = 0;
    const client = new UcpCheckoutClient(config, {
      now: () => now,
      fetcher: async () =>
        ++calls === 1
          ? profileResponse()
          : new Response(responseBody(canonicalJson(checkout({ id: "changed" }))), {
              status: 201,
              headers,
            }),
    });

    await expect(
      client.createCheckout(input, new AbortController().signal),
    ).rejects.toBeInstanceOf(UcpCheckoutError);
  });

  it.each([
    [400, "checkout_rejected"],
    [401, "merchant_authentication_failed"],
    [404, "merchant_not_found"],
    [409, "checkout_rejected"],
    [422, "checkout_rejected"],
    [500, "merchant_unavailable"],
  ])("maps unsigned merchant status %i without trusting its body", async (status, code) => {
    let calls = 0;
    const client = new UcpCheckoutClient(config, {
      now: () => now,
      fetcher: async () =>
        ++calls === 1
          ? profileResponse()
          : new Response('{"message":"untrusted"}', {
              status,
              headers: { "content-type": "application/json" },
            }),
    });

    await expect(
      client.createCheckout(input, new AbortController().signal),
    ).rejects.toMatchObject({ code });
  });

  it("uses the exact public key from discovery", () => {
    const jwk = merchantProfile().signing_keys[0];
    expect(importPublicJwk(jwk).asymmetricKeyDetails?.namedCurve).toBe("prime256v1");
  });
});
