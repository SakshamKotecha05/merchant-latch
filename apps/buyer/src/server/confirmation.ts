import { createHmac, timingSafeEqual } from "node:crypto";

import { z } from "zod";

import { canonicalJson } from "./ucp/canonical";

const originSchema = z.string().max(2_048).refine((value) => {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.origin === value &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash
    );
  } catch {
    return false;
  }
});

const claimsSchema = z
  .object({
    version: z.literal(1),
    requestId: z.string().uuid(),
    merchantOrigin: originSchema,
    variantId: z.string().min(1).max(256),
    quantity: z.number().int().min(1).max(20),
    unitPriceMinor: z.number().int().min(0).max(100_000_000),
    currency: z.string().regex(/^[A-Z]{3}$/),
    budgetMinor: z.number().int().min(1).max(100_000_000).optional(),
    expiresAt: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER),
  })
  .strict();

export type ConfirmationClaims = Readonly<z.infer<typeof claimsSchema>>;

export class ConfirmationError extends Error {
  readonly code: "confirmation_invalid" | "confirmation_expired";

  constructor(code: ConfirmationError["code"]) {
    super(`Purchase confirmation failed: ${code}.`);
    this.name = "ConfirmationError";
    this.code = code;
  }
}

const nowSeconds = (now: Date): number => Math.floor(now.getTime() / 1_000);
const validSecret = (secret: string): boolean => secret.length >= 32 && secret.length <= 512;
const mac = (payload: string, secret: string): Buffer =>
  createHmac("sha256", secret).update(payload, "ascii").digest();

const validateClaims = (value: unknown, now: Date): ConfirmationClaims => {
  const parsed = claimsSchema.safeParse(value);
  if (!parsed.success || !Number.isFinite(now.getTime())) {
    throw new ConfirmationError("confirmation_invalid");
  }
  const current = nowSeconds(now);
  if (parsed.data.expiresAt <= current) throw new ConfirmationError("confirmation_expired");
  if (parsed.data.expiresAt - current > 300) {
    throw new ConfirmationError("confirmation_invalid");
  }
  return Object.freeze(parsed.data);
};

export const issueConfirmation = (
  claims: ConfirmationClaims,
  secret: string,
  now: Date,
): string => {
  if (!validSecret(secret)) throw new ConfirmationError("confirmation_invalid");
  const validated = validateClaims(claims, now);
  const payload = Buffer.from(canonicalJson(validated)).toString("base64url");
  return `${payload}.${mac(payload, secret).toString("base64url")}`;
};

export const verifyConfirmation = (
  token: string,
  secret: string,
  now: Date,
): ConfirmationClaims => {
  if (!validSecret(secret) || token.length > 4_096) {
    throw new ConfirmationError("confirmation_invalid");
  }
  const segments = token.split(".");
  if (segments.length !== 2 || !segments[0] || !segments[1]) {
    throw new ConfirmationError("confirmation_invalid");
  }
  try {
    const supplied = Buffer.from(segments[1], "base64url");
    const expected = mac(segments[0], secret);
    if (
      supplied.length !== expected.length ||
      supplied.toString("base64url") !== segments[1] ||
      !timingSafeEqual(supplied, expected)
    ) {
      throw new ConfirmationError("confirmation_invalid");
    }
    const payload = Buffer.from(segments[0], "base64url");
    if (payload.toString("base64url") !== segments[0]) {
      throw new ConfirmationError("confirmation_invalid");
    }
    const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(payload));
    if (!timingSafeEqual(payload, Buffer.from(canonicalJson(value)))) {
      throw new ConfirmationError("confirmation_invalid");
    }
    return validateClaims(value, now);
  } catch (error) {
    if (error instanceof ConfirmationError) throw error;
    throw new ConfirmationError("confirmation_invalid");
  }
};
