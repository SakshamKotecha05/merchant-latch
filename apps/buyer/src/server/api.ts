import { z } from "zod";

import { BuyerApplicationError } from "./application";
import { CatalogError } from "./catalog";
import { ConfirmationError } from "./confirmation";
import { IntentExtractionError } from "./intent";
import { BuyerPlanningError } from "./planning";
import { UcpCheckoutError } from "./ucp/client";
import { MerchantDiscoveryError } from "./ucp/discovery";

const MAX_REQUEST_BYTES = 16 * 1_024;

export class BuyerApiError extends Error {
  readonly code: "invalid_json" | "invalid_media_type" | "request_too_large";

  constructor(code: BuyerApiError["code"]) {
    super(`Buyer API request failed: ${code}.`);
    this.name = "BuyerApiError";
    this.code = code;
  }
}

const jsonMediaType = (request: Request): boolean => {
  const mediaType = request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  return mediaType === "application/json" || Boolean(mediaType?.endsWith("+json"));
};

export const readApiJson = async (request: Request): Promise<unknown> => {
  if (!jsonMediaType(request)) throw new BuyerApiError("invalid_media_type");
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_REQUEST_BYTES)) {
    throw new BuyerApiError("request_too_large");
  }
  if (request.body === null) throw new BuyerApiError("invalid_json");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      length += part.value.byteLength;
      if (length > MAX_REQUEST_BYTES) throw new BuyerApiError("request_too_large");
      chunks.push(part.value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    throw new BuyerApiError("invalid_json");
  }
};

const messages = {
  invalid_json: [400, "The request body must be valid JSON.", true],
  invalid_media_type: [415, "The request must use a JSON content type.", true],
  request_too_large: [413, "The request body is too large.", true],
  invalid_input: [400, "Check the shopping request and try again.", true],
  model_unavailable: [503, "Shopping language is temporarily unavailable. Use manual selection.", true],
  model_output_invalid: [503, "Shopping language could not be interpreted. Use manual selection.", true],
  plan_invalid: [400, "Check the shopping request and try again.", true],
  no_match: [404, "No available merchant item matched those constraints.", true],
  inventory_unavailable: [409, "The requested quantity is no longer available.", true],
  budget_exceeded: [409, "The current merchant total exceeds the confirmed budget.", true],
  currency_mismatch: [409, "The merchant currency does not match the request.", true],
  catalog_unavailable: [502, "The merchant catalog is temporarily unavailable.", true],
  catalog_invalid: [502, "The merchant returned an invalid catalog response.", true],
  variant_not_found: [404, "The merchant item no longer exists.", true],
  confirmation_invalid: [400, "The purchase confirmation is invalid.", true],
  confirmation_expired: [409, "The purchase confirmation expired. Review the item again.", true],
  confirmation_required: [400, "Explicit confirmation is required before checkout.", true],
  merchant_mismatch: [400, "The confirmation does not belong to this merchant.", false],
  price_changed: [409, "The merchant price changed. Review the updated item before confirming again.", true],
  currency_changed: [409, "The merchant currency changed. Review the item again.", true],
  inventory_changed: [409, "Merchant inventory changed. Review the item again.", true],
  discovery_unavailable: [502, "Merchant discovery is temporarily unavailable.", true],
  discovery_invalid: [502, "The merchant discovery profile is invalid.", false],
  checkout_invalid: [400, "The confirmed checkout request is invalid.", true],
  checkout_outcome_unknown: [502, "The checkout outcome is unknown. Check before trying again.", false],
  checkout_rejected: [409, "The merchant rejected the checkout request.", true],
  merchant_authentication_failed: [502, "The merchant rejected buyer authentication.", false],
  merchant_not_found: [404, "The merchant checkout endpoint was not found.", true],
  merchant_response_invalid: [502, "The merchant checkout response could not be verified.", false],
  merchant_unavailable: [502, "The merchant checkout is temporarily unavailable.", true],
} as const;

type KnownError =
  | BuyerApiError
  | BuyerApplicationError
  | CatalogError
  | ConfirmationError
  | IntentExtractionError
  | BuyerPlanningError
  | MerchantDiscoveryError
  | UcpCheckoutError;

const knownError = (error: unknown): error is KnownError =>
  error instanceof BuyerApiError ||
  error instanceof BuyerApplicationError ||
  error instanceof CatalogError ||
  error instanceof ConfirmationError ||
  error instanceof IntentExtractionError ||
  error instanceof BuyerPlanningError ||
  error instanceof MerchantDiscoveryError ||
  error instanceof UcpCheckoutError;

export const apiResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Cache-Control": "no-store", "Content-Type": "application/json" },
  });

export const apiErrorResponse = (error: unknown): Response => {
  if (!knownError(error)) {
    return apiResponse(
      {
        error: {
          code: "internal_error",
          message: "The buyer service could not complete the request.",
          recoverable: false,
        },
      },
      500,
    );
  }
  const [status, message, recoverable] = messages[error.code];
  return apiResponse({ error: { code: error.code, message, recoverable } }, status);
};

export const parseBody = <T>(schema: z.ZodType<T>, value: unknown): T => {
  const parsed = schema.safeParse(value);
  if (!parsed.success) throw new IntentExtractionError("invalid_input");
  return parsed.data;
};
