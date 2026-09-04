import { z } from "zod";

import { decodeJson, readBoundedBody } from "./http";

const text = z.string().min(1).max(256);
const variantSchema = z
  .object({
    id: text,
    sku: text,
    size: z.string().max(64),
    color: z.string().max(64),
    unit_price_minor: z.number().int().min(0).max(100_000_000),
    currency: z.string().regex(/^[A-Z]{3}$/),
    available_quantity: z.number().int().min(0).max(1_000_000),
    inventory_version: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER),
  })
  .strict();
const itemSchema = z
  .object({
    id: text,
    name: text,
    description: z.string().max(2_000),
    variants: z.array(variantSchema).min(1).max(100),
  })
  .strict();
const catalogSchema = z
  .object({
    items: z.array(itemSchema).max(50),
    next_cursor: z.string().max(128).nullable(),
  })
  .strict();
const exactVariantSchema = variantSchema
  .extend({ product_id: text, product_name: text })
  .strict();

export type CatalogVariant = Readonly<{
  id: string;
  sku: string;
  size: string;
  color: string;
  unitPriceMinor: number;
  currency: string;
  availableQuantity: number;
  inventoryVersion: number;
}>;

export type ExactCatalogVariant = CatalogVariant &
  Readonly<{ productId: string; productName: string }>;

export type CatalogItem = Readonly<{
  id: string;
  name: string;
  description: string;
  variants: readonly CatalogVariant[];
}>;

export interface CatalogReader {
  search(query: string, signal: AbortSignal): Promise<readonly CatalogItem[]>;
  getVariant(variantId: string, signal: AbortSignal): Promise<ExactCatalogVariant>;
}

export class CatalogError extends Error {
  readonly code: "catalog_unavailable" | "catalog_invalid" | "variant_not_found";

  constructor(code: CatalogError["code"]) {
    super(`Catalog request failed: ${code}.`);
    this.name = "CatalogError";
    this.code = code;
  }
}

const variant = (value: z.infer<typeof variantSchema>): CatalogVariant =>
  Object.freeze({
    id: value.id,
    sku: value.sku,
    size: value.size,
    color: value.color,
    unitPriceMinor: value.unit_price_minor,
    currency: value.currency,
    availableQuantity: value.available_quantity,
    inventoryVersion: value.inventory_version,
  });

const jsonMediaType = (response: Response): boolean => {
  const mediaType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  return mediaType === "application/json" || Boolean(mediaType?.endsWith("+json"));
};

export class CatalogClient implements CatalogReader {
  constructor(
    private readonly gatewayUrl: URL,
    private readonly fetcher: typeof fetch = fetch,
  ) {}

  private async request(path: string, signal: AbortSignal): Promise<unknown> {
    const url = new URL(path, this.gatewayUrl);
    if (url.origin !== this.gatewayUrl.origin) throw new CatalogError("catalog_invalid");
    let response: Response;
    try {
      response = await this.fetcher(url, {
        method: "GET",
        redirect: "manual",
        headers: { Accept: "application/json" },
        signal: AbortSignal.any([signal, AbortSignal.timeout(3_000)]),
      });
    } catch {
      throw new CatalogError("catalog_unavailable");
    }
    if (response.status === 404) throw new CatalogError("variant_not_found");
    if (response.status !== 200 || !jsonMediaType(response)) {
      throw new CatalogError("catalog_unavailable");
    }
    try {
      return decodeJson(await readBoundedBody(response, 262_144));
    } catch {
      throw new CatalogError("catalog_invalid");
    }
  }

  async search(query: string, signal: AbortSignal): Promise<readonly CatalogItem[]> {
    const url = new URL("/ucp/shopping/catalog", this.gatewayUrl);
    url.searchParams.set("q", query);
    url.searchParams.set("limit", "50");
    const parsed = catalogSchema.safeParse(await this.request(`${url.pathname}${url.search}`, signal));
    if (!parsed.success) throw new CatalogError("catalog_invalid");
    const identifiers = parsed.data.items.flatMap((item) => item.variants.map((entry) => entry.id));
    if (new Set(identifiers).size !== identifiers.length) throw new CatalogError("catalog_invalid");
    return parsed.data.items.map((item) =>
      Object.freeze({
        id: item.id,
        name: item.name,
        description: item.description,
        variants: item.variants.map(variant),
      }),
    );
  }

  async getVariant(variantId: string, signal: AbortSignal): Promise<ExactCatalogVariant> {
    if (!variantId || variantId.length > 256) throw new CatalogError("catalog_invalid");
    const data = await this.request(
      `/ucp/shopping/catalog/variants/${encodeURIComponent(variantId)}`,
      signal,
    );
    const parsed = exactVariantSchema.safeParse(data);
    if (!parsed.success) throw new CatalogError("catalog_invalid");
    return Object.freeze({
      ...variant(parsed.data),
      productId: parsed.data.product_id,
      productName: parsed.data.product_name,
    });
  }
}
