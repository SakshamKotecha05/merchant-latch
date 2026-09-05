import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { cookieName, merchantBrowserOrigin, settings, validCheckout } from "./gateway";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("merchant gateway boundary", () => {
  it.each(["chk_1", "chk_checkout-123", "chk_A_B-C"])(
    "accepts a bounded checkout identifier: %s",
    (checkoutId) => {
      expect(validCheckout(checkoutId)).toBe(true);
    },
  );

  it.each(["", "checkout_1", "chk_", "chk_../secret", `chk_${"a".repeat(61)}`])(
    "rejects an unsafe checkout identifier: %s",
    (checkoutId) => {
      expect(validCheckout(checkoutId)).toBe(false);
    },
  );

  it("normalizes valid service origins", () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("PUBLIC_GATEWAY_URL", "https://gateway.example");
    vi.stubEnv("PUBLIC_MERCHANT_URL", "https://merchant.example");

    expect(settings()).toEqual({
      gateway: "https://gateway.example",
      merchant: "https://merchant.example",
    });
  });

  it.each([
    ["PUBLIC_GATEWAY_URL", "http://gateway.example"],
    ["PUBLIC_GATEWAY_URL", "https://user:pass@gateway.example"],
    ["PUBLIC_GATEWAY_URL", "https://gateway.example/path"],
    ["PUBLIC_MERCHANT_URL", "https://merchant.example?debug=true"],
  ])("rejects an unsafe production %s", (name, value) => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("PUBLIC_GATEWAY_URL", "https://gateway.example");
    vi.stubEnv("PUBLIC_MERCHANT_URL", "https://merchant.example");
    vi.stubEnv(name, value);

    expect(() => settings()).toThrowError("Invalid service origin configuration");
  });

  it("allows loopback HTTP origins outside production", () => {
    vi.stubEnv("NODE_ENV", "test");
    vi.stubEnv("PUBLIC_GATEWAY_URL", "http://127.0.0.1:8000");
    vi.stubEnv("PUBLIC_MERCHANT_URL", "http://localhost:3001");

    expect(settings()).toEqual({
      gateway: "http://127.0.0.1:8000",
      merchant: "http://localhost:3001",
    });
  });

  it("uses a host-only cookie name in production", () => {
    vi.stubEnv("NODE_ENV", "production");

    expect(cookieName("chk_1")).toBe("__Host-ml_chk_1");
  });

  it("rejects an unsafe checkout identifier before constructing a cookie name", () => {
    expect(() => cookieName("../secret")).toThrowError("Invalid checkout");
  });
});


describe("local merchant browser origin", () => {
  it("keeps production origin even when a local override exists", () => {
    vi.stubEnv("PUBLIC_GATEWAY_URL", "https://gateway.example");
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("PUBLIC_MERCHANT_URL", "https://merchant.example");
    vi.stubEnv("LOCAL_MERCHANT_URL", "http://localhost:3001");
    expect(merchantBrowserOrigin()).toBe("https://merchant.example");
  });
  it("allows a configured loopback browser while preserving the gateway origin", () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("PUBLIC_MERCHANT_URL", "https://merchant.example");
    vi.stubEnv("LOCAL_MERCHANT_URL", "http://localhost:3001");
    expect(merchantBrowserOrigin()).toBe("http://localhost:3001");
    expect(settings().merchant).toBe("https://merchant.example");
  });
  it.each(["http://evil.example", "http://localhost:3001/path", "http://user@localhost:3001"])("rejects unsafe local overrides: %s", (value) => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("LOCAL_MERCHANT_URL", value);
    expect(() => merchantBrowserOrigin()).toThrow();
  });
});
