import { describe, expect, it } from "vitest";

import {
  formatMinorAmount,
  initialBuyerFlowState,
  parseMajorAmountToMinor,
  reduceBuyerFlow,
  safeContinuationUrl,
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
    totalMinor: 499800,
    currency: "INR",
    availableQuantity: 7,
    inventoryVersion: 3,
    quantity: 2,
    score: 4,
  },
  alternatives: [],
  explanation: "2 x Night Run Trainer totals 499800 INR minor units using current merchant terms.",
  confirmationToken: "opaque-confirmation",
  expiresAt: "2026-09-05T12:05:00.000Z",
} as const;

describe("buyer flow state", () => {
  it("ignores a stale plan response after a newer request starts", () => {
    const first = reduceBuyerFlow(initialBuyerFlowState, {
      type: "operation_started",
      operation: "plan",
      requestId: 1,
    });
    const second = reduceBuyerFlow(first, {
      type: "operation_started",
      operation: "plan",
      requestId: 2,
    });

    const stale = reduceBuyerFlow(second, { type: "plan_succeeded", requestId: 1, plan });

    expect(stale).toEqual(second);
  });

  it("moves a current successful plan into review with confirmation reset", () => {
    const pending = reduceBuyerFlow(
      { ...initialBuyerFlowState, acknowledged: true },
      { type: "operation_started", operation: "plan", requestId: 7 },
    );

    const reviewed = reduceBuyerFlow(pending, { type: "plan_succeeded", requestId: 7, plan });

    expect(reviewed.step).toBe("review");
    expect(reviewed.plan).toEqual(plan);
    expect(reviewed.acknowledged).toBe(false);
    expect(reviewed.pending).toBeNull();
  });

  it("does not start checkout until the current plan is acknowledged", () => {
    const review = { ...initialBuyerFlowState, step: "review" as const, plan };

    const blocked = reduceBuyerFlow(review, {
      type: "operation_started",
      operation: "checkout",
      requestId: 8,
    });
    const allowed = reduceBuyerFlow(
      { ...review, acknowledged: true },
      { type: "operation_started", operation: "checkout", requestId: 8 },
    );

    expect(blocked).toEqual(review);
    expect(allowed.pending).toBe("checkout");
  });

  it("offers manual selection after a recoverable language failure", () => {
    const pending = reduceBuyerFlow(initialBuyerFlowState, {
      type: "operation_started",
      operation: "plan",
      requestId: 4,
    });

    const failed = reduceBuyerFlow(pending, {
      type: "operation_failed",
      requestId: 4,
      error: {
        code: "model_unavailable",
        message: "Shopping language is temporarily unavailable. Use manual selection.",
        recoverable: true,
      },
    });

    expect(failed.step).toBe("request");
    expect(failed.manualOpen).toBe(true);
    expect(failed.error?.code).toBe("model_unavailable");
  });

  it("stops automatic retry when checkout outcome is unknown", () => {
    const pending = {
      ...initialBuyerFlowState,
      step: "review" as const,
      plan,
      acknowledged: true,
      pending: "checkout" as const,
      activeRequestId: 9,
    };

    const failed = reduceBuyerFlow(pending, {
      type: "operation_failed",
      requestId: 9,
      error: {
        code: "checkout_outcome_unknown",
        message: "The checkout outcome is unknown. Check before trying again.",
        recoverable: false,
      },
    });

    expect(failed.pending).toBeNull();
    expect(failed.acknowledged).toBe(false);
    expect(failed.error?.recoverable).toBe(false);
  });

  it("moves only a current verified result into handoff", () => {
    const pending = {
      ...initialBuyerFlowState,
      step: "review" as const,
      plan,
      acknowledged: true,
      pending: "checkout" as const,
      activeRequestId: 11,
    };
    const result = {
      outcome: "requires_escalation",
      continueUrl: "https://merchant.example/continue/checkout_1",
    } as const;

    const handoff = reduceBuyerFlow(pending, {
      type: "checkout_succeeded",
      requestId: 11,
      result,
    });

    expect(handoff.step).toBe("handoff");
    expect(handoff.result).toEqual(result);
    expect(handoff.plan).toEqual(plan);
  });

  it("starts over without retaining confirmation or merchant data", () => {
    const restarted = reduceBuyerFlow(
      {
        ...initialBuyerFlowState,
        step: "handoff",
        plan,
        result: {
          outcome: "requires_escalation",
          continueUrl: "https://merchant.example/continue/checkout_1",
        },
      },
      { type: "start_over" },
    );

    expect(restarted).toEqual(initialBuyerFlowState);
  });
});

describe("buyer display boundaries", () => {
  it("formats minor units using the currency's actual fraction digits", () => {
    expect(formatMinorAmount(249900, "INR", "en-IN")).toBe("₹2,499.00");
    expect(formatMinorAmount(2499, "JPY", "ja-JP")).toBe("￥2,499");
    expect(formatMinorAmount(2499, "KWD", "en-US")).toBe("KWD 2.499");
  });

  it("converts typed major amounts without assuming two currency decimals", () => {
    expect(parseMajorAmountToMinor("3000", "INR")).toBe(300000);
    expect(parseMajorAmountToMinor("3000", "JPY")).toBe(3000);
    expect(parseMajorAmountToMinor("1.234", "KWD")).toBe(1234);
    expect(parseMajorAmountToMinor("1.2345", "KWD")).toBeNull();
    expect(parseMajorAmountToMinor("1.01", "JPY")).toBeNull();
    expect(parseMajorAmountToMinor("not-a-number", "INR")).toBeNull();
  });

  it("accepts only an HTTPS continuation without credentials or fragments", () => {
    expect(safeContinuationUrl("https://merchant.example/continue?id=1")).toBe(
      "https://merchant.example/continue?id=1",
    );
    expect(safeContinuationUrl("http://merchant.example/continue")).toBeNull();
    expect(safeContinuationUrl("https://user:pass@merchant.example/continue")).toBeNull();
    expect(safeContinuationUrl("https://merchant.example/continue#token")).toBeNull();
  });
});
