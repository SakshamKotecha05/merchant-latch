import { generateKeyPairSync } from "node:crypto";

import { describe, expect, it } from "vitest";

import { exportPublicJwk } from "./keys";
import { discoverMerchant, MerchantDiscoveryError } from "./discovery";

const merchantJwk = () => {
  const { privateKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  return exportPublicJwk(privateKey, "merchant-key-1");
};

const profile = () => ({
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
  signing_keys: [merchantJwk()],
});

const response = (body: unknown, init: ResponseInit = {}): Response =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json", ...init.headers },
    ...init,
  });

describe("discoverMerchant", () => {
  it("binds one compatible service and key to the configured origin", async () => {
    let request: RequestInit | undefined;
    let url = "";
    const identity = await discoverMerchant(
      { publicGatewayUrl: new URL("https://gateway.example/") },
      new AbortController().signal,
      async (input, init) => {
        url = String(input);
        request = init;
        return response(profile());
      },
    );

    expect(url).toBe("https://gateway.example/.well-known/ucp");
    expect(request).toMatchObject({ method: "GET", redirect: "manual" });
    expect(identity.origin).toBe("https://gateway.example");
    expect(identity.checkoutEndpoint.href).toBe(
      "https://gateway.example/ucp/shopping/checkout-sessions",
    );
    expect(identity.keyId).toBe("merchant-key-1");
    expect(identity.publicKey.asymmetricKeyDetails?.namedCurve).toBe("prime256v1");
  });

  it.each([
    ["redirect", profile(), { status: 302, headers: { location: "https://evil.example" } }],
    ["wrong media type", profile(), { headers: { "content-type": "text/plain" } }],
    ["oversized profile", profile(), { headers: { "content-length": "131073" } }],
    ["unsupported version", { ...profile(), ucp: { ...profile().ucp, version: "2026-08-25" } }, {}],
    [
      "cross-origin endpoint",
      {
        ...profile(),
        ucp: {
          ...profile().ucp,
          services: {
            "dev.ucp.shopping": [
              {
                version: "2026-04-08",
                transport: "rest",
                endpoint: "https://evil.example/ucp/shopping",
              },
            ],
          },
        },
      },
      {},
    ],
    [
      "missing checkout capability",
      { ...profile(), ucp: { ...profile().ucp, capabilities: {} } },
      {},
    ],
    ["multiple signing keys", { ...profile(), signing_keys: [merchantJwk(), merchantJwk()] }, {}],
  ])("rejects %s", async (_label, body, init) => {
    await expect(
      discoverMerchant(
        { publicGatewayUrl: new URL("https://gateway.example/") },
        new AbortController().signal,
        async () => response(body, init),
      ),
    ).rejects.toBeInstanceOf(MerchantDiscoveryError);
  });

  it("maps transport failures to a safe code", async () => {
    await expect(
      discoverMerchant(
        { publicGatewayUrl: new URL("https://gateway.example/") },
        new AbortController().signal,
        async () => {
          throw new Error("provider detail must not escape");
        },
      ),
    ).rejects.toMatchObject({ code: "discovery_unavailable" });
  });
});
