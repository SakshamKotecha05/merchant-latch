import { generateKeyPairSync } from "node:crypto";
import { once } from "node:events";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { createBuyerApplication } from "../../src/server/application";
import { CatalogClient } from "../../src/server/catalog";
import type { BuyerConfig } from "../../src/server/config";
import type { IntentExtractor } from "../../src/server/intent";
import { BuyerPlanner } from "../../src/server/planning";
import { UcpCheckoutClient } from "../../src/server/ucp/client";
import { exportPublicJwk } from "../../src/server/ucp/keys";

const workspace = new URL("../../../../", import.meta.url).pathname;
let processHandle: ChildProcess;
let localOrigin: string;
let fixtureError = "";
let checkoutFailure = "";

const availablePort = async (): Promise<number> => {
  const server = createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("No test port available.");
  server.close();
  await once(server, "close");
  return address.port;
};

const waitUntilReady = async (): Promise<void> => {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch(`${localOrigin}/test/state`);
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Gateway fixture did not start: ${fixtureError.slice(0, 2_000)}`);
};

describe("reference buyer against the FastAPI gateway", () => {
  const buyerKeys = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const config: BuyerConfig = {
    geminiApiKey: "not-used-in-e2e",
    geminiModel: "gemini-3.8-flash",
    buyerPrivateKeyPem: buyerKeys.privateKey
      .export({ format: "pem", type: "pkcs8" })
      .toString(),
    buyerKeyId: "buyer-e2e-key",
    sessionSecret: "e".repeat(32),
    publicBuyerUrl: new URL("https://buyer.example/"),
    publicGatewayUrl: new URL("https://gateway.example/"),
    publicMerchantUrl: new URL("https://gateway.example/"),
  };

  beforeAll(async () => {
    const port = await availablePort();
    localOrigin = `http://127.0.0.1:${port}`;
    processHandle = spawn(
      "uv",
      [
        "run",
        "--project",
        `${workspace}services/gateway`,
        "uvicorn",
        "gateway_fixture:app",
        "--app-dir",
        `${workspace}apps/buyer/test/e2e`,
        "--host",
        "127.0.0.1",
        "--port",
        String(port),
        "--log-level",
        "error",
      ],
      {
        cwd: workspace,
        env: {
          ...process.env,
          E2E_BUYER_JWK: JSON.stringify(exportPublicJwk(buyerKeys.privateKey, config.buyerKeyId)),
        },
        stdio: ["ignore", "ignore", "pipe"],
      },
    );
    processHandle.stderr?.on("data", (chunk: Buffer) => {
      fixtureError = `${fixtureError}${chunk.toString("utf8")}`.slice(-2_000);
    });
    await waitUntilReady();
  }, 15_000);

  afterAll(async () => {
    if (!processHandle?.killed) {
      processHandle.kill("SIGTERM");
      await Promise.race([
        once(processHandle, "exit"),
        new Promise((resolve) => setTimeout(resolve, 2_000)),
      ]);
    }
  });

  it("plans, confirms, signs, pins, verifies, and escalates without payment", async () => {
    const remappedFetch: typeof fetch = async (input, init) => {
      const source = new URL(String(input));
      const response = await fetch(`${localOrigin}${source.pathname}${source.search}`, init);
      if (source.pathname.endsWith("/checkout-sessions") && !response.ok) {
        checkoutFailure = `${response.status} ${await response.clone().text()}`;
      }
      return response;
    };
    const catalog = new CatalogClient(config.publicGatewayUrl, remappedFetch);
    const extractor: IntentExtractor = {
      extract: async () => ({
        searchQuery: "road running shoe",
        quantity: 1,
        color: "black",
        size: "42",
        budgetMinor: 300_000,
        currency: "INR",
      }),
    };
    const application = createBuyerApplication({
      extractor,
      planner: new BuyerPlanner({
        catalog,
        sessionSecret: config.sessionSecret,
        merchantOrigin: config.publicGatewayUrl.origin,
      }),
      catalog,
      checkout: new UcpCheckoutClient(config, { fetcher: remappedFetch }),
      sessionSecret: config.sessionSecret,
      merchantOrigin: config.publicGatewayUrl.origin,
    });
    const signal = new AbortController().signal;
    const purchasePlan = await application.plan({ text: "one black road runner size 42" }, signal);

    expect(purchasePlan.recommended).toMatchObject({
      variantId: "var_stride_42_black",
      totalMinor: 249_900,
      currency: "INR",
    });
    let checkout;
    try {
      checkout = await application.createCheckout(
        { confirmed: true, confirmationToken: purchasePlan.confirmationToken },
        signal,
      );
    } catch (error) {
      throw new Error(`Checkout failed: ${checkoutFailure}`, { cause: error });
    }
    expect(checkout).toMatchObject({
      outcome: "requires_escalation",
      continueUrl: "https://gateway.example/checkout/chk_e2e",
    });

    const replay = await application.createCheckout(
      { confirmed: true, confirmationToken: purchasePlan.confirmationToken },
      signal,
    );
    expect(replay.outcome).toBe("requires_escalation");

    await expect(
      application.createCheckout(
        { confirmed: true, confirmationToken: `${purchasePlan.confirmationToken}x` },
        signal,
      ),
    ).rejects.toMatchObject({ code: "confirmation_invalid" });

    await fetch(`${localOrigin}/test/inventory/0`, { method: "POST" });
    await expect(
      application.createCheckout(
        { confirmed: true, confirmationToken: purchasePlan.confirmationToken },
        signal,
      ),
    ).rejects.toMatchObject({ code: "inventory_changed" });

    const state = await (await fetch(`${localOrigin}/test/state`)).json();
    expect(state).toEqual({
      pinned: true,
      checkout_calls: 1,
      exchange_outcomes: ["accepted", "replayed"],
      payments_created: false,
    });
  }, 15_000);
});
