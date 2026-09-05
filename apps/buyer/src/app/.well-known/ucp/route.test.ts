import { generateKeyPairSync } from "node:crypto";

import { beforeEach, describe, expect, it, vi } from "vitest";

const privateKey = generateKeyPairSync("ec", {
  namedCurve: "prime256v1",
  privateKeyEncoding: { format: "pem", type: "pkcs8" },
  publicKeyEncoding: { format: "pem", type: "spki" },
}).privateKey;

describe("GET /.well-known/ucp", () => {
  beforeEach(() => {
    vi.resetModules();
    process.env.GEMINI_API_KEY = "gemini-test-key-not-a-real-secret";
    process.env.UCP_BUYER_PRIVATE_KEY = privateKey;
    process.env.UCP_BUYER_KEY_ID = "buyer-p256-2026-01";
    process.env.BUYER_SESSION_SECRET = "s".repeat(32);
    process.env.PUBLIC_BUYER_URL = "https://buyer.example";
    process.env.PUBLIC_GATEWAY_URL = "https://gateway.example";
  });

  it("publishes only the buyer's compatible public signing key", async () => {
    const { GET } = await import("./route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(response.headers.get("cache-control")).toBe("public, max-age=300");
    expect(body.ucp).toEqual({
      version: "2026-04-08",
      services: {},
      capabilities: {},
      payment_handlers: {},
    });
    expect(body.signing_keys).toHaveLength(1);
    expect(body.signing_keys[0]).toMatchObject({
      kid: "buyer-p256-2026-01",
      kty: "EC",
      crv: "P-256",
      alg: "ES256",
      use: "sig",
    });
    expect(body.signing_keys[0]).not.toHaveProperty("d");
  });
});
