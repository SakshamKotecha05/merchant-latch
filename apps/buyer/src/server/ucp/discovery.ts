import type { KeyObject } from "node:crypto";

import { z } from "zod";

import type { BuyerConfig } from "../config";
import { decodeJson, readBoundedBody } from "../http";
import { importPublicJwk } from "./keys";

const UCP_VERSION = "2026-04-08";
const MAX_PROFILE_BYTES = 128 * 1_024;

const compatibleEntry = z
  .object({
    version: z.literal(UCP_VERSION),
    transport: z.literal("rest").optional(),
    endpoint: z.string().max(2_048).optional(),
  })
  .passthrough();

const profileSchema = z
  .object({
    ucp: z
      .object({
        version: z.literal(UCP_VERSION),
        services: z
          .object({ "dev.ucp.shopping": z.array(compatibleEntry).length(1) })
          .passthrough(),
        capabilities: z
          .object({
            "dev.ucp.shopping.checkout": z
              .array(z.object({ version: z.literal(UCP_VERSION) }).passthrough())
              .length(1),
          })
          .passthrough(),
      })
      .passthrough(),
    signing_keys: z.array(z.unknown()).length(1),
  })
  .passthrough();

export type MerchantIdentity = Readonly<{
  origin: string;
  checkoutEndpoint: URL;
  keyId: string;
  publicKey: KeyObject;
}>;

export class MerchantDiscoveryError extends Error {
  readonly code: "discovery_unavailable" | "discovery_invalid";

  constructor(code: MerchantDiscoveryError["code"]) {
    super(`Merchant discovery failed: ${code}.`);
    this.name = "MerchantDiscoveryError";
    this.code = code;
  }
}

const jsonMediaType = (response: Response): boolean => {
  const mediaType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  return mediaType === "application/json" || Boolean(mediaType?.endsWith("+json"));
};

const checkoutEndpoint = (value: string, gateway: URL): URL => {
  let endpoint: URL;
  try {
    endpoint = new URL(value);
  } catch {
    throw new MerchantDiscoveryError("discovery_invalid");
  }
  if (
    endpoint.origin !== gateway.origin ||
    endpoint.protocol !== gateway.protocol ||
    endpoint.username ||
    endpoint.password ||
    endpoint.search ||
    endpoint.hash ||
    !endpoint.pathname.startsWith("/")
  ) {
    throw new MerchantDiscoveryError("discovery_invalid");
  }
  return new URL(`${endpoint.pathname.replace(/\/+$/, "")}/checkout-sessions`, endpoint.origin);
};

export const discoverMerchant = async (
  config: Pick<BuyerConfig, "publicGatewayUrl">,
  signal: AbortSignal,
  fetcher: typeof fetch = fetch,
): Promise<MerchantIdentity> => {
  const discoveryUrl = new URL("/.well-known/ucp", config.publicGatewayUrl);
  let response: Response;
  try {
    response = await fetcher(discoveryUrl, {
      method: "GET",
      redirect: "manual",
      headers: { Accept: "application/json" },
      signal: AbortSignal.any([signal, AbortSignal.timeout(3_000)]),
    });
  } catch {
    throw new MerchantDiscoveryError("discovery_unavailable");
  }
  if (response.status !== 200 || !jsonMediaType(response)) {
    throw new MerchantDiscoveryError(
      response.status >= 300 && response.status < 400
        ? "discovery_invalid"
        : "discovery_unavailable",
    );
  }
  let document: unknown;
  try {
    document = decodeJson(await readBoundedBody(response, MAX_PROFILE_BYTES));
  } catch {
    throw new MerchantDiscoveryError("discovery_invalid");
  }
  const parsed = profileSchema.safeParse(document);
  if (!parsed.success) throw new MerchantDiscoveryError("discovery_invalid");
  const service = parsed.data.ucp.services["dev.ucp.shopping"][0];
  if (service.transport !== "rest" || service.endpoint === undefined) {
    throw new MerchantDiscoveryError("discovery_invalid");
  }
  try {
    const publicKey = importPublicJwk(parsed.data.signing_keys[0]);
    const keyId = (parsed.data.signing_keys[0] as { kid?: unknown }).kid;
    if (typeof keyId !== "string") throw new TypeError();
    return Object.freeze({
      origin: config.publicGatewayUrl.origin,
      checkoutEndpoint: checkoutEndpoint(service.endpoint, config.publicGatewayUrl),
      keyId,
      publicKey,
    });
  } catch (error) {
    if (error instanceof MerchantDiscoveryError) throw error;
    throw new MerchantDiscoveryError("discovery_invalid");
  }
};
