import { randomUUID } from "node:crypto";

import { z } from "zod";

import type {
  CatalogItem,
  CatalogReader,
  CatalogVariant,
  ExactCatalogVariant,
} from "./catalog";
import { issueConfirmation } from "./confirmation";
import type { ShoppingIntent } from "./intent";

export type Candidate = Readonly<{
  productId: string;
  productName: string;
  variantId: string;
  sku: string;
  size: string;
  color: string;
  unitPriceMinor: number;
  totalMinor: number;
  currency: string;
  availableQuantity: number;
  inventoryVersion: number;
  quantity: number;
  score: number;
}>;

export type PurchasePlan = Readonly<{
  recommended: Candidate;
  alternatives: readonly Candidate[];
  explanation: string;
  confirmationToken: string;
  expiresAt: string;
}>;

const manualSchema = z
  .object({
    variantId: z.string().min(1).max(256),
    quantity: z.number().int().min(1).max(20),
    budgetMinor: z.number().int().min(1).max(100_000_000).optional(),
    currency: z.string().regex(/^[A-Z]{3}$/).optional(),
  })
  .strict();

export type ManualPlanInput = z.input<typeof manualSchema>;

export class BuyerPlanningError extends Error {
  readonly code:
    | "plan_invalid"
    | "no_match"
    | "inventory_unavailable"
    | "budget_exceeded"
    | "currency_mismatch";

  constructor(code: BuyerPlanningError["code"]) {
    super(`Purchase planning failed: ${code}.`);
    this.name = "BuyerPlanningError";
    this.code = code;
  }
}

const normalized = (value: string): string => value.trim().toLocaleLowerCase("en-US");
const words = (value: string): readonly string[] =>
  normalized(value)
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);

const toCandidate = (
  productId: string,
  productName: string,
  description: string,
  variant: CatalogVariant,
  intent: ShoppingIntent,
): Candidate | null => {
  if (intent.quantity > variant.availableQuantity) return null;
  if (intent.color && normalized(variant.color) !== intent.color) return null;
  if (intent.size && normalized(variant.size) !== intent.size) return null;
  if (intent.currency && variant.currency !== intent.currency) return null;
  const totalMinor = variant.unitPriceMinor * intent.quantity;
  if (!Number.isSafeInteger(totalMinor) || totalMinor > 100_000_000) return null;
  if (intent.budgetMinor !== undefined && totalMinor > intent.budgetMinor) return null;
  const haystack = new Set(words(`${productName} ${description} ${variant.sku}`));
  const score = words(intent.searchQuery).filter((word) => haystack.has(word)).length;
  return Object.freeze({
    productId,
    productName,
    variantId: variant.id,
    sku: variant.sku,
    size: variant.size,
    color: variant.color,
    unitPriceMinor: variant.unitPriceMinor,
    totalMinor,
    currency: variant.currency,
    availableQuantity: variant.availableQuantity,
    inventoryVersion: variant.inventoryVersion,
    quantity: intent.quantity,
    score,
  });
};

export const rankCandidates = (
  intent: ShoppingIntent,
  items: readonly CatalogItem[],
): readonly Candidate[] =>
  items
    .flatMap((item) =>
      item.variants.map((variant) =>
        toCandidate(item.id, item.name, item.description, variant, intent),
      ),
    )
    .filter((candidate): candidate is Candidate => candidate !== null)
    .sort(
      (left, right) =>
        right.score - left.score ||
        left.totalMinor - right.totalMinor ||
        left.variantId.localeCompare(right.variantId, "en"),
    );

const exactCandidate = (
  variant: ExactCatalogVariant,
  quantity: number,
  budgetMinor?: number,
  currency?: string,
): Candidate => {
  if (quantity > variant.availableQuantity) throw new BuyerPlanningError("inventory_unavailable");
  if (currency && currency !== variant.currency) throw new BuyerPlanningError("currency_mismatch");
  const totalMinor = variant.unitPriceMinor * quantity;
  if (!Number.isSafeInteger(totalMinor) || totalMinor > 100_000_000) {
    throw new BuyerPlanningError("plan_invalid");
  }
  if (budgetMinor !== undefined && totalMinor > budgetMinor) {
    throw new BuyerPlanningError("budget_exceeded");
  }
  return Object.freeze({
    productId: variant.productId,
    productName: variant.productName,
    variantId: variant.id,
    sku: variant.sku,
    size: variant.size,
    color: variant.color,
    unitPriceMinor: variant.unitPriceMinor,
    totalMinor,
    currency: variant.currency,
    availableQuantity: variant.availableQuantity,
    inventoryVersion: variant.inventoryVersion,
    quantity,
    score: 0,
  });
};

export class BuyerPlanner {
  private readonly catalog: CatalogReader;
  private readonly sessionSecret: string;
  private readonly merchantOrigin: string;
  private readonly requestId: () => string;

  constructor(options: {
    catalog: CatalogReader;
    sessionSecret: string;
    merchantOrigin: string;
    requestId?: () => string;
  }) {
    this.catalog = options.catalog;
    this.sessionSecret = options.sessionSecret;
    this.merchantOrigin = options.merchantOrigin;
    this.requestId = options.requestId ?? randomUUID;
  }

  private purchasePlan(
    recommended: Candidate,
    alternatives: readonly Candidate[],
    budgetMinor: number | undefined,
    now: Date,
  ): PurchasePlan {
    const expiresAt = new Date(now.getTime() + 300_000);
    const confirmationToken = issueConfirmation(
      {
        version: 1,
        requestId: this.requestId(),
        merchantOrigin: this.merchantOrigin,
        variantId: recommended.variantId,
        quantity: recommended.quantity,
        unitPriceMinor: recommended.unitPriceMinor,
        currency: recommended.currency,
        ...(budgetMinor === undefined ? {} : { budgetMinor }),
        expiresAt: Math.floor(expiresAt.getTime() / 1_000),
      },
      this.sessionSecret,
      now,
    );
    return Object.freeze({
      recommended,
      alternatives,
      explanation: `${recommended.quantity} x ${recommended.productName} totals ${recommended.totalMinor} ${recommended.currency} minor units using current merchant terms.`,
      confirmationToken,
      expiresAt: expiresAt.toISOString(),
    });
  }

  async plan(intent: ShoppingIntent, now: Date, signal: AbortSignal): Promise<PurchasePlan> {
    const candidates = rankCandidates(intent, await this.catalog.search(intent.searchQuery, signal));
    const recommended = candidates[0];
    if (!recommended) throw new BuyerPlanningError("no_match");
    return this.purchasePlan(recommended, candidates.slice(1, 4), intent.budgetMinor, now);
  }

  async planManual(input: ManualPlanInput, now: Date, signal: AbortSignal): Promise<PurchasePlan> {
    const parsed = manualSchema.safeParse(input);
    if (!parsed.success) throw new BuyerPlanningError("plan_invalid");
    const selected = await this.catalog.getVariant(parsed.data.variantId, signal);
    const recommended = exactCandidate(
      selected,
      parsed.data.quantity,
      parsed.data.budgetMinor,
      parsed.data.currency,
    );
    return this.purchasePlan(recommended, [], parsed.data.budgetMinor, now);
  }
}
