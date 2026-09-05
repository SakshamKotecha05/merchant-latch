import { generateKeyPairSync, verify } from "node:crypto";
import { spawnSync } from "node:child_process";

import { describe, expect, it } from "vitest";

import pythonVector from "../../../test/fixtures/ucp-python-vectors.json";
import { exportPublicJwk, importPublicJwk, loadP256PrivateKey } from "./keys";
import {
  contentDigest,
  signUcpRequest,
  UcpSignatureError,
  verifyContentDigest,
  verifyUcpResponse,
} from "./signatures";

const encoder = new TextEncoder();

const p256Pem = (): string =>
  generateKeyPairSync("ec", {
    namedCurve: "prime256v1",
    privateKeyEncoding: { format: "pem", type: "pkcs8" },
    publicKeyEncoding: { format: "pem", type: "spki" },
  }).privateKey;

describe("UCP content digest", () => {
  it("uses the canonical RFC 9530 SHA-256 representation", () => {
    expect(contentDigest(encoder.encode("hello"))).toBe(
      "sha-256=:LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ=:",
    );
  });

  it.each([
    [undefined],
    ["sha-512=:YWJjZA==:"],
    ["sha-256=:YWJjZA==:, sha-512=:YWJjZA==:"],
    ["sha-256=:not base64!:"],
    ["sha-256=:YWJjZA==:"],
  ])("rejects missing, malformed, unsupported, or mismatched digest %s", (header) => {
    expect(() => verifyContentDigest(encoder.encode("hello"), header)).toThrowError(
      UcpSignatureError,
    );
  });
});

describe("UCP request signing", () => {
  it("signs the exact gateway component order with a raw P-256 signature", () => {
    const privateKey = loadP256PrivateKey(p256Pem());
    const body = encoder.encode('{"line_items":[{"item":{"id":"var_1"},"quantity":2}]}');
    const headers = {
      "content-type": "application/json",
      "idempotency-key": "idem-123",
      "ucp-agent": 'profile="https://buyer.example/.well-known/ucp"',
    };

    const signed = signUcpRequest({
      method: "POST",
      url: new URL("https://gateway.example/ucp/shopping/checkout-sessions"),
      headers,
      body,
      privateKey,
      keyId: "buyer-p256-2026-01",
      created: 1_788_500_000,
      expires: 1_788_500_300,
      nonce: "nonce-123",
    });

    expect(signed["Content-Digest"]).toBe(contentDigest(body));
    expect(signed["Signature-Input"]).toBe(
      'sig1=("@method" "@authority" "@path" "ucp-agent" "idempotency-key" "content-digest" "content-type");created=1788500000;keyid="buyer-p256-2026-01";expires=1788500300;nonce="nonce-123"',
    );
    const signatureBase = [
      '"@method": POST',
      '"@authority": gateway.example',
      '"@path": /ucp/shopping/checkout-sessions',
      '"ucp-agent": profile="https://buyer.example/.well-known/ucp"',
      '"idempotency-key": idem-123',
      `"content-digest": ${contentDigest(body)}`,
      '"content-type": application/json',
      `"@signature-params": ${signed["Signature-Input"].slice(5)}`,
    ].join("\n");
    const signature = Buffer.from(signed.Signature.slice(6, -1), "base64");
    expect(signature).toHaveLength(64);
    expect(
      verify(
        "sha256",
        Buffer.from(signatureBase),
        { key: privateKey, dsaEncoding: "ieee-p1363" },
        signature,
      ),
    ).toBe(true);
  });

  it("covers a query and rejects missing idempotency", () => {
    const input = {
      method: "POST",
      url: new URL("https://gateway.example/path?version=1"),
      headers: {
        "content-type": "application/json",
        "ucp-agent": 'profile="https://buyer.example/.well-known/ucp"',
      },
      body: encoder.encode("{}"),
      privateKey: loadP256PrivateKey(p256Pem()),
      keyId: "buyer-key",
      created: 1_788_500_000,
      expires: 1_788_500_300,
      nonce: "nonce-123",
    };

    expect(() => signUcpRequest(input)).toThrowError(
      expect.objectContaining({ code: "idempotency_missing" }),
    );
    const signed = signUcpRequest({
      ...input,
      headers: { ...input.headers, "idempotency-key": "idem-123" },
    });
    expect(signed["Signature-Input"]).toContain('"@query"');
  });

  it("produces a request accepted by the Python gateway verifier", () => {
    const privateKey = loadP256PrivateKey(p256Pem());
    const created = Math.floor(Date.now() / 1_000);
    const body = encoder.encode('{"line_items":[{"item":{"id":"var_1"},"quantity":1}]}');
    const requestHeaders = {
      "content-type": "application/json",
      "idempotency-key": "interop-idem-1",
      "ucp-agent": 'profile="https://buyer.example/.well-known/ucp"',
    };
    const signed = signUcpRequest({
      method: "POST",
      url: new URL("https://gateway.example/ucp/shopping/checkout-sessions"),
      headers: requestHeaders,
      body,
      privateKey,
      keyId: "buyer-interop-key",
      created,
      expires: created + 300,
      nonce: "interop-nonce-1",
    });
    const script = [
      "import base64, json, sys, httpx",
      "from acsa.security.ucp_signatures import import_public_jwk, verify_request",
      "value = json.load(sys.stdin)",
      "request = httpx.Request(value['method'], value['url'], headers=value['headers'], content=base64.b64decode(value['body']))",
      "verified = verify_request(request, public_key=import_public_jwk(value['jwk']), expected_key_id=value['key_id'])",
      "print(verified.nonce)",
    ].join("\n");
    const result = spawnSync("uv", ["run", "python", "-c", script], {
      cwd: new URL("../../../../../services/gateway", import.meta.url),
      input: JSON.stringify({
        method: "POST",
        url: "https://gateway.example/ucp/shopping/checkout-sessions",
        headers: { ...requestHeaders, ...signed },
        body: Buffer.from(body).toString("base64"),
        jwk: exportPublicJwk(privateKey, "buyer-interop-key"),
        key_id: "buyer-interop-key",
      }),
      encoding: "utf8",
    });

    expect(result.stderr).toBe("");
    expect(result.status).toBe(0);
    expect(result.stdout.trim()).toBe("interop-nonce-1");
  });
});

describe("UCP response verification", () => {
  const validInput = () => ({
    status: pythonVector.status_code,
    headers: pythonVector.headers,
    body: Buffer.from(pythonVector.body_hex, "hex"),
    publicKey: importPublicJwk(pythonVector.public_jwk),
    expectedKeyId: pythonVector.key_id,
    now: pythonVector.now,
  });

  it("verifies a stored response emitted by the Python gateway", () => {
    expect(() => verifyUcpResponse(validInput())).not.toThrow();
  });

  it.each([
    ["status", { status: 200 }],
    ["body", { body: encoder.encode("changed") }],
    ["key id", { expectedKeyId: "other-key" }],
    ["expiry", { now: 1_788_500_306 }],
  ])("rejects a changed %s", (_label, change) => {
    expect(() => verifyUcpResponse({ ...validInput(), ...change })).toThrowError(
      UcpSignatureError,
    );
  });

  it.each([
    ["extra signature label", { signature: `${pythonVector.headers.signature}, sig2=:AA==:` }],
    [
      "extra covered component",
      {
        "signature-input": pythonVector.headers["signature-input"].replace(
          '"content-type")',
          '"content-type" "date")',
        ),
      },
    ],
    [
      "oversized lifetime",
      {
        "signature-input": pythonVector.headers["signature-input"].replace(
          "expires=1788500300",
          "expires=1788500301",
        ),
      },
    ],
    ["malformed signature", { signature: "sig1=:not base64!:" }],
  ])("rejects %s before trusting the response", (_label, changedHeaders) => {
    expect(() =>
      verifyUcpResponse({
        ...validInput(),
        headers: { ...pythonVector.headers, ...changedHeaders },
      }),
    ).toThrowError(UcpSignatureError);
  });
});
