import { describe, expect, it, vi } from "vitest";

import type { CatalogReader, ExactCatalogVariant } from "./catalog";
import { issueConfirmation } from "./confirmation";
import type { IntentExtractor } from "./intent";
import type { BuyerPlanner, PurchasePlan } from "./planning";
import {
  BuyerApplicationError,
  createBuyerApplication,
  type CheckoutWriter,
} from "./application";

const now = new Date("2026-09-04T12:00:00Z");
const secret = "s".repeat(32);
const variant: ExactCatalogVariant = {
  id: "var_1",
  productId: "prod_1",
  productName: "Stride Runner",
  sku: "STRIDE-1",
  size: "42",
  color: "Black",
  unitPriceMinor: 249_900,
  currency: "INR",
  availableQuantity: 3,
  inventoryVersion: 5,
};
const plan: PurchasePlan = {
  recommended: {
    productId: "prod_1",
    productName: "Stride Runner",
    variantId: "var_1",
    sku: "STRIDE-1",
    size: "42",
    color: "Black",
    unitPriceMinor: 249_900,
    totalMinor: 249_900,
    currency: "INR",
    availableQuantity: 3,
    inventoryVersion: 5,
    quantity: 1,
    score: 1,
  },
  alternatives: [],
  explanation: "Current merchant terms.",
  confirmationToken: "token",
  expiresAt: "2026-09-04T12:05:00.000Z",
};

const token = (changes: Record<string, unknown> = {}, issuedAt = now): string =>
  issueConfirmation(
    {
      version: 1,
      requestId: "123e4567-e89b-42d3-a456-426614174000",
      merchantOrigin: "https://gateway.example",
      variantId: "var_1",
      quantity: 1,
      unitPriceMinor: 249_900,
      currency: "INR",
      budgetMinor: 300_000,
      expiresAt: Math.floor(issuedAt.getTime() / 1_000) + 300,
      ...changes,
    },
    secret,
    issuedAt,
  );

const dependencies = (variantResult: ExactCatalogVariant = variant) => {
  const extractor: IntentExtractor = {
    extract: vi.fn(async () => ({ searchQuery: "runner", quantity: 1 })),
  };
  const planner = {
    plan: vi.fn(async () => plan),
    planManual: vi.fn(async () => plan),
  } as unknown as BuyerPlanner;
  const catalog: CatalogReader = {
    search: vi.fn(async () => []),
    getVariant: vi.fn(async () => variantResult),
  };
  const checkout: CheckoutWriter = {
    createCheckout: vi.fn(async () => ({
      data: {} as never,
      outcome: "requires_escalation" as const,
      continueUrl: "https://gateway.example/checkout/chk_1",
    })),
  };
  return { extractor, planner, catalog, checkout };
};

describe("BuyerApplication", () => {
  it("connects natural-language and manual planning without giving the model checkout access", async () => {
    const deps = dependencies();
    const app = createBuyerApplication({
      ...deps,
      sessionSecret: secret,
      merchantOrigin: "https://gateway.example",
      now: () => now,
    });
    const signal = new AbortController().signal;

    expect(await app.plan({ text: "one black runner" }, signal)).toBe(plan);
    expect(deps.extractor.extract).toHaveBeenCalledWith("one black runner", signal);
    expect(deps.planner.plan).toHaveBeenCalledWith(
      { searchQuery: "runner", quantity: 1 },
      now,
      signal,
    );
    expect(await app.planManual({ variantId: "var_1", quantity: 1 }, signal)).toBe(plan);
  });

  it("requires an explicit true confirmation", async () => {
    const app = createBuyerApplication({
      ...dependencies(),
      sessionSecret: secret,
      merchantOrigin: "https://gateway.example",
      now: () => now,
    });

    await expect(
      app.createCheckout(
        { confirmed: false, confirmationToken: token() },
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ code: "confirmation_required" });
  });

  it.each([
    ["tampered", `${token()}x`, now, "confirmation_invalid"],
    ["expired", token(), new Date("2026-09-04T12:05:01Z"), "confirmation_expired"],
  ])("rejects a %s confirmation", async (_label, confirmationToken, current, code) => {
    const app = createBuyerApplication({
      ...dependencies(),
      sessionSecret: secret,
      merchantOrigin: "https://gateway.example",
      now: () => current,
    });

    await expect(
      app.createCheckout({ confirmed: true, confirmationToken }, new AbortController().signal),
    ).rejects.toMatchObject({ code });
  });

  it.each([
    ["price", { ...variant, unitPriceMinor: 250_000 }, "price_changed"],
    ["currency", { ...variant, currency: "USD" }, "currency_changed"],
    ["stock", { ...variant, availableQuantity: 0 }, "inventory_changed"],
  ])("re-fetches and rejects a %s change", async (_label, currentVariant, code) => {
    const deps = dependencies(currentVariant);
    const app = createBuyerApplication({
      ...deps,
      sessionSecret: secret,
      merchantOrigin: "https://gateway.example",
      now: () => now,
    });

    await expect(
      app.createCheckout(
        { confirmed: true, confirmationToken: token() },
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ code });
    expect(deps.checkout.createCheckout).not.toHaveBeenCalled();
  });

  it("rechecks the total budget and forwards only confirmation-bound fields", async () => {
    const deps = dependencies();
    const app = createBuyerApplication({
      ...deps,
      sessionSecret: secret,
      merchantOrigin: "https://gateway.example",
      now: () => now,
    });
    const signal = new AbortController().signal;

    const result = await app.createCheckout(
      { confirmed: true, confirmationToken: token() },
      signal,
    );

    expect(deps.catalog.getVariant).toHaveBeenCalledWith("var_1", signal);
    expect(deps.checkout.createCheckout).toHaveBeenCalledWith(
      {
        requestId: "123e4567-e89b-42d3-a456-426614174000",
        variantId: "var_1",
        quantity: 1,
        budgetMinor: 300_000,
      },
      signal,
    );
    expect(result.outcome).toBe("requires_escalation");
  });

  it("rejects an unsafe or over-budget recomputed total", async () => {
    const app = createBuyerApplication({
      ...dependencies(),
      sessionSecret: secret,
      merchantOrigin: "https://gateway.example",
      now: () => now,
    });

    await expect(
      app.createCheckout(
        {
          confirmed: true,
          confirmationToken: token({ budgetMinor: 200_000 }),
        },
        new AbortController().signal,
      ),
    ).rejects.toBeInstanceOf(BuyerApplicationError);
  });
});
