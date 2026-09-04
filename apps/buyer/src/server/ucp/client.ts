import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";

import type { CheckoutResponse } from "@ucp-js/sdk";
import { z } from "zod";

import type { BuyerConfig } from "../config";
import { decodeJson, readBoundedBody } from "../http";
import { canonicalJson } from "./canonical";
import { discoverMerchant } from "./discovery";
import { loadP256PrivateKey } from "./keys";
import { signUcpRequest, verifyUcpResponse } from "./signatures";

const require = createRequire(import.meta.url);
const { CheckoutResponseSchema } = require("@ucp-js/sdk") as typeof import("@ucp-js/sdk");

const UCP_VERSION = "2026-04-08";
const MAX_CHECKOUT_BYTES = 512 * 1_024;

const fetchBody = (bytes: Uint8Array): ArrayBuffer =>
  bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;

const confirmedPurchaseSchema = z
  .object({
    requestId: z.string().uuid(),
    variantId: z.string().min(1).max(256),
    quantity: z.number().int().min(1).max(20),
    budgetMinor: z.number().int().min(1).max(100_000_000).optional(),
  })
  .strict();

export type ConfirmedPurchase = Readonly<z.infer<typeof confirmedPurchaseSchema>>;

export type VerifiedCheckoutResult = Readonly<{
  data: CheckoutResponse;
  outcome: CheckoutResponse["status"];
  continueUrl?: string;
}>;

export class UcpCheckoutError extends Error {
  readonly code:
    | "checkout_invalid"
    | "checkout_outcome_unknown"
    | "checkout_rejected"
    | "merchant_authentication_failed"
    | "merchant_not_found"
    | "merchant_response_invalid"
    | "merchant_unavailable";

  constructor(code: UcpCheckoutError["code"]) {
    super(`UCP checkout failed: ${code}.`);
    this.name = "UcpCheckoutError";
    this.code = code;
  }
}

type ClientOptions = Readonly<{
  fetcher?: typeof fetch;
  now?: () => Date;
  nonce?: () => string;
}>;

const jsonMediaType = (response: Response): boolean => {
  const mediaType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  return mediaType === "application/json" || Boolean(mediaType?.endsWith("+json"));
};

const errorForStatus = (status: number): UcpCheckoutError => {
  if (status === 401 || status === 403) {
    return new UcpCheckoutError("merchant_authentication_failed");
  }
  if (status === 404) return new UcpCheckoutError("merchant_not_found");
  if (status === 400 || status === 409 || status === 422) {
    return new UcpCheckoutError("checkout_rejected");
  }
  return new UcpCheckoutError("merchant_unavailable");
};

const validatedContinueUrl = (
  value: string | undefined,
  merchantOrigin: string,
  required: boolean,
): string | undefined => {
  if (value === undefined) {
    if (required) throw new UcpCheckoutError("merchant_response_invalid");
    return undefined;
  }
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.origin !== merchantOrigin ||
      url.username ||
      url.password ||
      url.hash
    ) {
      throw new Error();
    }
    return url.href;
  } catch {
    throw new UcpCheckoutError("merchant_response_invalid");
  }
};

export class UcpCheckoutClient {
  private readonly fetcher: typeof fetch;
  private readonly now: () => Date;
  private readonly nonce: () => string;

  constructor(
    private readonly config: BuyerConfig,
    options: ClientOptions = {},
  ) {
    this.fetcher = options.fetcher ?? fetch;
    this.now = options.now ?? (() => new Date());
    this.nonce = options.nonce ?? randomUUID;
  }

  async createCheckout(
    input: ConfirmedPurchase,
    signal: AbortSignal,
  ): Promise<VerifiedCheckoutResult> {
    const parsedInput = confirmedPurchaseSchema.safeParse(input);
    if (!parsedInput.success) throw new UcpCheckoutError("checkout_invalid");

    const merchant = await discoverMerchant(this.config, signal, this.fetcher);
    const body = canonicalJson({
      line_items: [
        { item: { id: parsedInput.data.variantId }, quantity: parsedInput.data.quantity },
      ],
      ...(parsedInput.data.budgetMinor === undefined
        ? {}
        : { budget_minor: parsedInput.data.budgetMinor }),
    });
    const profileUrl = new URL("/.well-known/ucp", this.config.publicBuyerUrl).href;
    const headers = {
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": parsedInput.data.requestId,
      "UCP-Agent": `profile="${profileUrl}"`,
    };
    const time = this.now();
    const created = Math.floor(time.getTime() / 1_000);
    if (!Number.isFinite(time.getTime())) throw new UcpCheckoutError("checkout_invalid");
    let signed: ReturnType<typeof signUcpRequest>;
    try {
      signed = signUcpRequest({
        method: "POST",
        url: merchant.checkoutEndpoint,
        headers,
        body,
        privateKey: loadP256PrivateKey(this.config.buyerPrivateKeyPem),
        keyId: this.config.buyerKeyId,
        created,
        expires: created + 300,
        nonce: this.nonce(),
      });
    } catch {
      throw new UcpCheckoutError("checkout_invalid");
    }

    let response: Response;
    try {
      response = await this.fetcher(merchant.checkoutEndpoint, {
        method: "POST",
        redirect: "manual",
        headers: { ...headers, ...signed },
        body: fetchBody(body),
        signal: AbortSignal.any([signal, AbortSignal.timeout(5_000)]),
      });
    } catch {
      throw new UcpCheckoutError("checkout_outcome_unknown");
    }

    let responseBody: Uint8Array;
    try {
      responseBody = await readBoundedBody(response, MAX_CHECKOUT_BYTES);
    } catch {
      throw new UcpCheckoutError("merchant_response_invalid");
    }
    if (response.status < 200 || response.status >= 300) throw errorForStatus(response.status);
    if (!jsonMediaType(response)) throw new UcpCheckoutError("merchant_response_invalid");
    try {
      verifyUcpResponse({
        status: response.status,
        headers: response.headers,
        body: responseBody,
        publicKey: merchant.publicKey,
        expectedKeyId: merchant.keyId,
        now: Math.floor(this.now().getTime() / 1_000),
      });
    } catch {
      throw new UcpCheckoutError("merchant_response_invalid");
    }

    let value: unknown;
    try {
      value = decodeJson(responseBody);
    } catch {
      throw new UcpCheckoutError("merchant_response_invalid");
    }
    const checkout = CheckoutResponseSchema.safeParse(value);
    if (
      !checkout.success ||
      checkout.data.ucp.version !== UCP_VERSION ||
      !checkout.data.ucp.capabilities?.["dev.ucp.shopping.checkout"]?.some(
        (capability) => capability.version === UCP_VERSION,
      )
    ) {
      throw new UcpCheckoutError("merchant_response_invalid");
    }
    const continueUrl = validatedContinueUrl(
      checkout.data.continue_url,
      merchant.origin,
      checkout.data.status === "requires_escalation",
    );
    return Object.freeze({
      data: checkout.data,
      outcome: checkout.data.status,
      ...(continueUrl === undefined ? {} : { continueUrl }),
    });
  }
}
