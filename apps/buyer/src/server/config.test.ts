import { describe, expect, it } from "vitest";

import { BuyerConfigurationError, loadBuyerConfig } from "./config";

const validEnvironment = (): NodeJS.ProcessEnv => ({
  NODE_ENV: "test",
  GEMINI_API_KEY: "gemini-test-key-not-a-real-secret",
  UCP_BUYER_PRIVATE_KEY: "fixture-private-key-material-".repeat(2),
  UCP_BUYER_KEY_ID: "buyer-p256-2026-01",
  BUYER_SESSION_SECRET: "s".repeat(32),
  PUBLIC_BUYER_URL: "https://buyer.example",
  PUBLIC_GATEWAY_URL: "https://gateway.example",
  PUBLIC_MERCHANT_URL: "https://merchant.example",
});

describe("loadBuyerConfig", () => {
  it("returns normalized configuration with the stable Gemini default", () => {
    const config = loadBuyerConfig(validEnvironment());

    expect(config.geminiModel).toBe("gemini-3.5-flash-lite");
    expect(config.publicBuyerUrl.href).toBe("https://buyer.example/");
    expect(config.publicGatewayUrl.href).toBe("https://gateway.example/");
    expect(config.publicMerchantUrl.href).toBe("https://merchant.example/");
    expect(Object.isFrozen(config)).toBe(true);
  });

  it("names missing fields without exposing any configured secret", () => {
    const environment = validEnvironment();
    const sentinel = environment.GEMINI_API_KEY as string;
    delete environment.UCP_BUYER_PRIVATE_KEY;

    expect(() => loadBuyerConfig(environment)).toThrowError(BuyerConfigurationError);
    try {
      loadBuyerConfig(environment);
    } catch (error) {
      expect(String(error)).toContain("UCP_BUYER_PRIVATE_KEY");
      expect(String(error)).not.toContain(sentinel);
    }
  });

  it.each([
    ["BUYER_SESSION_SECRET", "short"],
    ["UCP_BUYER_KEY_ID", ""],
    ["UCP_BUYER_KEY_ID", "k".repeat(256)],
    ["PUBLIC_BUYER_URL", "http://buyer.example"],
    ["PUBLIC_GATEWAY_URL", "https://user:pass@gateway.example"],
    ["PUBLIC_GATEWAY_URL", "https://gateway.example/#fragment"],
    ["PUBLIC_MERCHANT_URL", "http://merchant.example"],
  ])("rejects an invalid %s value", (name, value) => {
    const environment = validEnvironment();
    environment[name] = value;

    expect(() => loadBuyerConfig(environment)).toThrowError(name);
  });

  it("allows loopback HTTP URLs for local development", () => {
    const environment = validEnvironment();
    environment.PUBLIC_BUYER_URL = "http://localhost:3000";
    environment.PUBLIC_GATEWAY_URL = "http://127.0.0.1:8000";

    const config = loadBuyerConfig(environment);

    expect(config.publicBuyerUrl.origin).toBe("http://localhost:3000");
    expect(config.publicGatewayUrl.origin).toBe("http://127.0.0.1:8000");
  });

  it("uses an explicit bounded Gemini model identifier", () => {
    const environment = validEnvironment();
    environment.GEMINI_MODEL = "gemini-3.7-flash";

    expect(loadBuyerConfig(environment).geminiModel).toBe("gemini-3.7-flash");

    environment.GEMINI_MODEL = "bad model";
    expect(() => loadBuyerConfig(environment)).toThrowError("GEMINI_MODEL");
  });
});
