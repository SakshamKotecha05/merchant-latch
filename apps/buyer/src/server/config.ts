import { z } from "zod";

const environmentSchema = z
  .object({
    GEMINI_API_KEY: z.string().min(10).max(512),
    GEMINI_MODEL: z
      .string()
      .min(1)
      .max(128)
      .regex(/^[a-z0-9][a-z0-9._-]*$/)
      .default("gemini-3.5-flash-lite"),
    UCP_BUYER_PRIVATE_KEY: z.string().min(32).max(16_384),
    UCP_BUYER_KEY_ID: z
      .string()
      .min(1)
      .max(255)
      .regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]*$/),
    BUYER_SESSION_SECRET: z.string().min(32).max(512),
    PUBLIC_BUYER_URL: z.string().min(1).max(2_048),
    PUBLIC_GATEWAY_URL: z.string().min(1).max(2_048),
    PUBLIC_MERCHANT_URL: z.string().min(1).max(2_048),
  })
  .passthrough();

export type BuyerConfig = Readonly<{
  geminiApiKey: string;
  geminiModel: string;
  buyerPrivateKeyPem: string;
  buyerKeyId: string;
  sessionSecret: string;
  publicBuyerUrl: URL;
  publicGatewayUrl: URL;
  publicMerchantUrl: URL;
}>;

export class BuyerConfigurationError extends Error {
  constructor(fields: readonly string[]) {
    super(`Invalid buyer configuration fields: ${[...new Set(fields)].sort().join(", ")}`);
    this.name = "BuyerConfigurationError";
  }
}

const isLoopback = (hostname: string): boolean =>
  hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";

const publicUrl = (name: string, value: string): URL => {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new BuyerConfigurationError([name]);
  }
  if (
    (url.protocol !== "https:" && !(url.protocol === "http:" && isLoopback(url.hostname))) ||
    url.username !== "" ||
    url.password !== "" ||
    url.hash !== "" ||
    url.search !== "" ||
    url.pathname !== "/"
  ) {
    throw new BuyerConfigurationError([name]);
  }
  return url;
};

export const loadBuyerConfig = (environment: NodeJS.ProcessEnv): BuyerConfig => {
  const parsed = environmentSchema.safeParse(environment);
  if (!parsed.success) {
    throw new BuyerConfigurationError(
      parsed.error.issues.map((issue) => String(issue.path[0] ?? "environment")),
    );
  }
  return Object.freeze({
    geminiApiKey: parsed.data.GEMINI_API_KEY,
    geminiModel: parsed.data.GEMINI_MODEL,
    buyerPrivateKeyPem: parsed.data.UCP_BUYER_PRIVATE_KEY,
    buyerKeyId: parsed.data.UCP_BUYER_KEY_ID,
    sessionSecret: parsed.data.BUYER_SESSION_SECRET,
    publicBuyerUrl: publicUrl("PUBLIC_BUYER_URL", parsed.data.PUBLIC_BUYER_URL),
    publicGatewayUrl: publicUrl("PUBLIC_GATEWAY_URL", parsed.data.PUBLIC_GATEWAY_URL),
    publicMerchantUrl: publicUrl("PUBLIC_MERCHANT_URL", parsed.data.PUBLIC_MERCHANT_URL),
  });
};
