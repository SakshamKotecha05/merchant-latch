import { describe, expect, it } from "vitest";

import {
  ConfirmationError,
  issueConfirmation,
  verifyConfirmation,
  type ConfirmationClaims,
} from "./confirmation";

const secret = "s".repeat(32);
const now = new Date("2026-09-04T12:00:00Z");

const claims = (): ConfirmationClaims => ({
  version: 1,
  requestId: "550e8400-e29b-41d4-a716-446655440000",
  merchantOrigin: "https://gateway.example",
  variantId: "var_stride_42_black",
  quantity: 2,
  unitPriceMinor: 249_900,
  currency: "INR",
  budgetMinor: 500_000,
  expiresAt: Math.floor(now.getTime() / 1_000) + 300,
});

describe("confirmation tokens", () => {
  it("round trips strict purchase facts in canonical base64url form", () => {
    const token = issueConfirmation(claims(), secret, now);

    expect(token).toMatch(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
    expect(verifyConfirmation(token, secret, now)).toEqual(claims());
  });

  it.each(["payload", "signature"])("rejects a changed %s", (part) => {
    const token = issueConfirmation(claims(), secret, now);
    const segments = token.split(".");
    const index = part === "payload" ? 0 : 1;
    segments[index] = `${segments[index]?.slice(0, -1)}A`;

    expect(() => verifyConfirmation(segments.join("."), secret, now)).toThrowError(
      ConfirmationError,
    );
  });

  it("rejects expiry and lifetimes beyond five minutes", () => {
    const token = issueConfirmation(claims(), secret, now);
    const exactExpiry = new Date(now.getTime() + 300_000);
    const expired = new Date(now.getTime() + 301_000);

    expect(() => verifyConfirmation(token, secret, exactExpiry)).toThrowError(
      "confirmation_expired",
    );
    expect(() => verifyConfirmation(token, secret, expired)).toThrowError("confirmation_expired");
    expect(() =>
      issueConfirmation({ ...claims(), expiresAt: claims().expiresAt + 1 }, secret, now),
    ).toThrowError("confirmation_invalid");
  });

  it.each([
    [{ quantity: 0 }],
    [{ unitPriceMinor: Number.MAX_SAFE_INTEGER }],
    [{ currency: "usd" }],
    [{ merchantOrigin: "https://user:pass@gateway.example" }],
    [{ requestId: "not-a-uuid" }],
  ])("rejects invalid protected claims", (change) => {
    expect(() => issueConfirmation({ ...claims(), ...change }, secret, now)).toThrowError(
      "confirmation_invalid",
    );
  });
});
