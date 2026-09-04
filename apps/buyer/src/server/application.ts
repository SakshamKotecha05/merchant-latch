import type { CatalogReader } from "./catalog";
import { verifyConfirmation } from "./confirmation";
import type { IntentExtractor } from "./intent";
import type { ManualPlanInput, PurchasePlan } from "./planning";
import type { ConfirmedPurchase, VerifiedCheckoutResult } from "./ucp/client";

export type PlanInput = Readonly<{ text: string }>;
export type ConfirmInput = Readonly<{ confirmationToken: string; confirmed: boolean }>;

interface Planner {
  plan(
    intent: Awaited<ReturnType<IntentExtractor["extract"]>>,
    now: Date,
    signal: AbortSignal,
  ): Promise<PurchasePlan>;
  planManual(input: ManualPlanInput, now: Date, signal: AbortSignal): Promise<PurchasePlan>;
}

export interface CheckoutWriter {
  createCheckout(
    input: ConfirmedPurchase,
    signal: AbortSignal,
  ): Promise<VerifiedCheckoutResult>;
}

export interface BuyerApplication {
  plan(input: PlanInput, signal: AbortSignal): Promise<PurchasePlan>;
  planManual(input: ManualPlanInput, signal: AbortSignal): Promise<PurchasePlan>;
  createCheckout(input: ConfirmInput, signal: AbortSignal): Promise<VerifiedCheckoutResult>;
}

export class BuyerApplicationError extends Error {
  readonly code:
    | "confirmation_required"
    | "merchant_mismatch"
    | "price_changed"
    | "currency_changed"
    | "inventory_changed"
    | "budget_exceeded";

  constructor(code: BuyerApplicationError["code"]) {
    super(`Buyer operation failed: ${code}.`);
    this.name = "BuyerApplicationError";
    this.code = code;
  }
}

export const createBuyerApplication = (dependencies: {
  extractor: IntentExtractor;
  planner: Planner;
  catalog: CatalogReader;
  checkout: CheckoutWriter;
  sessionSecret: string;
  merchantOrigin: string;
  now?: () => Date;
}): BuyerApplication => {
  const currentTime = dependencies.now ?? (() => new Date());
  return Object.freeze({
    plan: async (input: PlanInput, signal: AbortSignal): Promise<PurchasePlan> => {
      const intent = await dependencies.extractor.extract(input.text, signal);
      return dependencies.planner.plan(intent, currentTime(), signal);
    },
    planManual: (
      input: ManualPlanInput,
      signal: AbortSignal,
    ): Promise<PurchasePlan> => dependencies.planner.planManual(input, currentTime(), signal),
    createCheckout: async (
      input: ConfirmInput,
      signal: AbortSignal,
    ): Promise<VerifiedCheckoutResult> => {
      if (input.confirmed !== true) throw new BuyerApplicationError("confirmation_required");
      const claims = verifyConfirmation(
        input.confirmationToken,
        dependencies.sessionSecret,
        currentTime(),
      );
      if (claims.merchantOrigin !== dependencies.merchantOrigin) {
        throw new BuyerApplicationError("merchant_mismatch");
      }
      const variant = await dependencies.catalog.getVariant(claims.variantId, signal);
      if (variant.unitPriceMinor !== claims.unitPriceMinor) {
        throw new BuyerApplicationError("price_changed");
      }
      if (variant.currency !== claims.currency) {
        throw new BuyerApplicationError("currency_changed");
      }
      if (variant.availableQuantity < claims.quantity) {
        throw new BuyerApplicationError("inventory_changed");
      }
      const totalMinor = variant.unitPriceMinor * claims.quantity;
      if (!Number.isSafeInteger(totalMinor) || totalMinor > 100_000_000) {
        throw new BuyerApplicationError("price_changed");
      }
      if (claims.budgetMinor !== undefined && totalMinor > claims.budgetMinor) {
        throw new BuyerApplicationError("budget_exceeded");
      }
      return dependencies.checkout.createCheckout(
        {
          requestId: claims.requestId,
          variantId: claims.variantId,
          quantity: claims.quantity,
          ...(claims.budgetMinor === undefined ? {} : { budgetMinor: claims.budgetMinor }),
        },
        signal,
      );
    },
  });
};
