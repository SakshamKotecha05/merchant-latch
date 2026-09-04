"use server";

import { z } from "zod";

import { apiErrorResponse } from "../server/api";
import { getBuyerApplication } from "../server/runtime";
import type { ManualPlanInput, PurchasePlan } from "../server/planning";
import type {
  BuyerActionError,
  BuyerActionResult,
  BuyerCheckoutView,
} from "./buyer-flow-model";

const errorSchema = z
  .object({
    error: z
      .object({
        code: z.string().min(1).max(64),
        message: z.string().min(1).max(256),
        recoverable: z.boolean(),
      })
      .strict(),
  })
  .strict();

const internalError: BuyerActionError = Object.freeze({
  code: "internal_error",
  message: "The buyer service could not complete the request.",
  recoverable: false,
});

const failure = async <T>(error: unknown): Promise<BuyerActionResult<T>> => {
  const parsed = errorSchema.safeParse(await apiErrorResponse(error).json());
  return { ok: false, error: parsed.success ? parsed.data.error : internalError };
};

const actionSignal = (): AbortSignal => AbortSignal.timeout(12_000);

export async function planPurchase(text: string): Promise<BuyerActionResult<PurchasePlan>> {
  try {
    return { ok: true, data: await getBuyerApplication().plan({ text }, actionSignal()) };
  } catch (error) {
    return failure(error);
  }
}

export async function planManualPurchase(
  input: ManualPlanInput,
): Promise<BuyerActionResult<PurchasePlan>> {
  try {
    return { ok: true, data: await getBuyerApplication().planManual(input, actionSignal()) };
  } catch (error) {
    return failure(error);
  }
}

export async function confirmPurchase(
  confirmationToken: string,
): Promise<BuyerActionResult<BuyerCheckoutView>> {
  try {
    const checkout = await getBuyerApplication().createCheckout(
      { confirmationToken, confirmed: true },
      actionSignal(),
    );
    return {
      ok: true,
      data: {
        outcome: checkout.outcome,
        ...(checkout.continueUrl === undefined ? {} : { continueUrl: checkout.continueUrl }),
      },
    };
  } catch (error) {
    return failure(error);
  }
}
