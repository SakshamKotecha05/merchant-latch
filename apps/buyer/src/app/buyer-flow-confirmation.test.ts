import { describe, expect, it } from "vitest";

import {
  canSubmitCheckout,
  handoffLinkAttributes,
  initialBuyerFlowState,
} from "./buyer-flow-model";

const plan = {
  recommended: {
    productId: "product_shoe",
    productName: "Night Run Trainer",
    variantId: "variant_black_42",
    sku: "NRT-BLK-42",
    size: "42",
    color: "Black",
    unitPriceMinor: 249900,
    totalMinor: 249900,
    currency: "INR",
    availableQuantity: 7,
    inventoryVersion: 3,
    quantity: 1,
    score: 4,
  },
  alternatives: [],
  explanation: "1 x Night Run Trainer totals 249900 INR minor units using current merchant terms.",
  confirmationToken: "opaque-confirmation",
  expiresAt: "2026-09-05T12:05:00.000Z",
} as const;

describe("explicit checkout confirmation", () => {
  it("allows checkout only for an acknowledged current review with no pending operation", () => {
    expect(canSubmitCheckout(initialBuyerFlowState)).toBe(false);
    expect(
      canSubmitCheckout({
        ...initialBuyerFlowState,
        step: "review",
        plan,
        acknowledged: false,
      }),
    ).toBe(false);
    expect(
      canSubmitCheckout({
        ...initialBuyerFlowState,
        step: "review",
        plan,
        acknowledged: true,
      }),
    ).toBe(true);
    expect(
      canSubmitCheckout({
        ...initialBuyerFlowState,
        step: "review",
        plan,
        acknowledged: true,
        pending: "checkout",
      }),
    ).toBe(false);
  });
});

describe("verified merchant handoff", () => {
  it("turns a safe continuation into an explicit new-tab link without a referrer", () => {
    expect(handoffLinkAttributes("https://merchant.example/continue?id=1")).toEqual({
      href: "https://merchant.example/continue?id=1",
      target: "_blank",
      rel: "noopener noreferrer",
      referrerPolicy: "no-referrer",
    });
  });

  it("does not create a link for an unsafe or missing continuation", () => {
    expect(handoffLinkAttributes(undefined)).toBeNull();
    expect(handoffLinkAttributes("http://merchant.example/continue")).toBeNull();
    expect(handoffLinkAttributes("https://merchant.example/continue#secret")).toBeNull();
  });
});
