import { describe, expect, it } from "vitest";

import type { BuyerApplication } from "../../../server/application";
import { BuyerApplicationError } from "../../../server/application";
import { createCheckoutPost } from "./checkouts/route";
import { createManualPlanPost } from "./plan/manual/route";
import { createPlanPost } from "./plan/route";

const plan = {
  recommended: {},
  alternatives: [],
  explanation: "Merchant terms.",
  confirmationToken: "token",
  expiresAt: "2026-09-04T12:05:00.000Z",
} as never;

const application = (error?: Error): BuyerApplication => ({
  plan: async () => {
    if (error) throw error;
    return plan;
  },
  planManual: async () => {
    if (error) throw error;
    return plan;
  },
  createCheckout: async () => {
    if (error) throw error;
    return {
      data: {} as never,
      outcome: "requires_escalation",
      continueUrl: "https://gateway.example/checkout/chk_1",
    };
  },
});

const request = (body: string, contentType = "application/json"): Request =>
  new Request("https://buyer.example/api/buyer/plan", {
    method: "POST",
    headers: { "content-type": contentType },
    body,
  });

describe("buyer API routes", () => {
  it("accepts bounded strict JSON for all three routes and disables caching", async () => {
    const responses = await Promise.all([
      createPlanPost(application())(request('{"text":"one black runner"}')),
      createManualPlanPost(application())(
        request('{"variantId":"var_1","quantity":1,"currency":"INR"}'),
      ),
      createCheckoutPost(application())(
        request('{"confirmationToken":"token","confirmed":true}'),
      ),
    ]);

    expect(responses.map((response) => response.status)).toEqual([200, 200, 201]);
    for (const response of responses) {
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(response.headers.get("content-type")).toBe("application/json");
    }
  });

  it.each([
    ["wrong media type", request("{}", "text/plain"), 415],
    ["malformed JSON", request("{"), 400],
    [
      "oversized JSON",
      new Request("https://buyer.example/api/buyer/plan", {
        method: "POST",
        headers: { "content-type": "application/json", "content-length": "16385" },
        body: "{}",
      }),
      413,
    ],
    ["invalid shape", request('{"text":"ok","extra":true}'), 400],
  ])("rejects %s", async (_label, input, status) => {
    const response = await createPlanPost(application())(input);
    expect(response.status).toBe(status);
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("maps known errors to allowlisted text without reflecting provider details", async () => {
    const failure = new BuyerApplicationError("price_changed");
    failure.stack = "sensitive stack";
    const response = await createCheckoutPost(application(failure))(
      request('{"confirmationToken":"token","confirmed":true}'),
    );
    const body = await response.json();

    expect(response.status).toBe(409);
    expect(body).toEqual({
      error: {
        code: "price_changed",
        message: "The merchant price changed. Review the updated item before confirming again.",
        recoverable: true,
      },
    });
    expect(JSON.stringify(body)).not.toContain("sensitive");
  });

  it("hides unknown error messages", async () => {
    const response = await createPlanPost(application(new Error("provider secret body")))(
      request('{"text":"one black runner"}'),
    );
    const body = await response.json();

    expect(response.status).toBe(500);
    expect(JSON.stringify(body)).not.toContain("provider secret body");
  });
});
