import { z } from "zod";

const normalizedOptional = (maximum: number) =>
  z
    .string()
    .trim()
    .min(1)
    .max(maximum)
    .transform((value) => value.toLocaleLowerCase("en-US"))
    .optional();

export const ShoppingIntentSchema = z
  .object({
    searchQuery: z.string().trim().min(1).max(128),
    quantity: z.number().int().min(1).max(20),
    color: normalizedOptional(64),
    size: normalizedOptional(64),
    budgetMinor: z.number().int().min(1).max(100_000_000).optional(),
    currency: z
      .string()
      .trim()
      .transform((value) => value.toUpperCase())
      .pipe(z.string().regex(/^[A-Z]{3}$/))
      .optional(),
  })
  .strict();

export type ShoppingIntent = z.infer<typeof ShoppingIntentSchema>;

export type IntentErrorCode = "invalid_input" | "model_unavailable" | "model_output_invalid";

export class IntentExtractionError extends Error {
  readonly code: IntentErrorCode;

  constructor(code: IntentErrorCode) {
    super(`Intent extraction failed: ${code}.`);
    this.name = "IntentExtractionError";
    this.code = code;
  }
}

export const parseIntentInput = (value: unknown): string => {
  if (typeof value !== "string") throw new IntentExtractionError("invalid_input");
  const normalized = value.trim();
  if (
    normalized.length < 3 ||
    normalized.length > 500 ||
    /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(normalized)
  ) {
    throw new IntentExtractionError("invalid_input");
  }
  return normalized;
};

export interface IntentExtractor {
  extract(text: string, signal: AbortSignal): Promise<ShoppingIntent>;
}
