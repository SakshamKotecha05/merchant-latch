import { createPrivateKey, createPublicKey, generateKeyPairSync } from "node:crypto";

import { describe, expect, it } from "vitest";

import { exportPublicJwk, importPublicJwk, loadP256PrivateKey } from "./keys";

const p256Pem = (): string =>
  generateKeyPairSync("ec", {
    namedCurve: "prime256v1",
    privateKeyEncoding: { format: "pem", type: "pkcs8" },
    publicKeyEncoding: { format: "pem", type: "spki" },
  }).privateKey;

describe("P-256 UCP keys", () => {
  it("exports a public-only canonical UCP JWK and imports it", () => {
    const privateKey = loadP256PrivateKey(p256Pem());
    const jwk = exportPublicJwk(privateKey, "buyer-p256-2026-01");

    expect(jwk).toEqual({
      kid: "buyer-p256-2026-01",
      kty: "EC",
      crv: "P-256",
      x: expect.stringMatching(/^[A-Za-z0-9_-]{43}$/),
      y: expect.stringMatching(/^[A-Za-z0-9_-]{43}$/),
      use: "sig",
      alg: "ES256",
    });
    expect("d" in jwk).toBe(false);
    expect(importPublicJwk(jwk).asymmetricKeyType).toBe("ec");
  });

  it("rejects a private key on another curve", () => {
    const pem = generateKeyPairSync("ec", {
      namedCurve: "secp384r1",
      privateKeyEncoding: { format: "pem", type: "pkcs8" },
      publicKeyEncoding: { format: "pem", type: "spki" },
    }).privateKey;

    expect(() => loadP256PrivateKey(pem)).toThrowError("P-256");
  });

  it("rejects non-private and malformed key material", () => {
    const publicPem = createPublicKey(createPrivateKey(p256Pem())).export({
      format: "pem",
      type: "spki",
    });

    expect(() => loadP256PrivateKey(publicPem.toString())).toThrowError("private");
    expect(() => loadP256PrivateKey("not a key")).toThrowError("private");
  });

  it.each([
    ["private material", { d: "secret" }],
    ["wrong algorithm", { alg: "ES384" }],
    ["wrong curve", { crv: "P-384" }],
    ["padded coordinate", { x: "A".repeat(42) + "=" }],
    ["short coordinate", { y: "AA" }],
    ["unknown field", { extra: "value" }],
  ])("rejects a JWK with %s", (_label, change) => {
    const valid = exportPublicJwk(loadP256PrivateKey(p256Pem()), "buyer-key");

    expect(() => importPublicJwk({ ...valid, ...change })).toThrowError("public key");
  });
});
