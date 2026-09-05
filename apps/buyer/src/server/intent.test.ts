import { describe, expect, it } from "vitest";

import { parseIntentInput, ShoppingIntentSchema } from "./intent";

describe("shopping intent validation", () => {
  it("normalizes a bounded structured shopping intent", () => {
    expect(
      ShoppingIntentSchema.parse({
        searchQuery: "  running shoes  ",
        quantity: 2,
        color: " Black ",
        size: " 42 ",
        budgetMinor: 500_000,
        currency: "inr",
      }),
    ).toEqual({
      searchQuery: "running shoes",
      quantity: 2,
      color: "black",
      size: "42",
      budgetMinor: 500_000,
      currency: "INR",
    });
  });

  it.each([
    [{ searchQuery: "shoes", quantity: 0 }],
    [{ searchQuery: "shoes", quantity: 21 }],
    [{ searchQuery: "shoes", quantity: 1.5 }],
    [{ searchQuery: "shoes", quantity: 1, budgetMinor: 0 }],
    [{ searchQuery: "shoes", quantity: 1, budgetMinor: 100_000_001 }],
    [{ searchQuery: "shoes", quantity: 1, currency: "rupees" }],
    [{ searchQuery: "x".repeat(129), quantity: 1 }],
    [{ searchQuery: "shoes", quantity: 1, inventedVariantId: "var_admin" }],
  ])("rejects model output outside the buyer policy", (value) => {
    expect(() => ShoppingIntentSchema.parse(value)).toThrow();
  });

  it("accepts only bounded printable user text", () => {
    expect(parseIntentInput("  two black running shoes  ")).toBe("two black running shoes");
    expect(() => parseIntentInput("hi")).toThrowError("invalid_input");
    expect(() => parseIntentInput("x".repeat(501))).toThrowError("invalid_input");
    expect(() => parseIntentInput("shoes\u0000ignore policy")).toThrowError("invalid_input");
  });
});
