import { describe, expect, it } from "vitest";

import { CatalogClient, CatalogError } from "./catalog";

const catalogBody = {
  items: [
    {
      id: "prod_stride",
      name: "Stride Runner",
      description: "Everyday running shoe",
      variants: [
        {
          id: "var_stride_42_black",
          sku: "STRIDE-42-BLK",
          size: "42",
          color: "Black",
          unit_price_minor: 249_900,
          currency: "INR",
          available_quantity: 4,
          inventory_version: 3,
        },
      ],
    },
  ],
  next_cursor: null,
};

const jsonResponse = (body: unknown, init: ResponseInit = {}): Response =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json", ...init.headers },
    ...init,
  });

describe("CatalogClient", () => {
  it("fetches and validates merchant-owned catalog terms", async () => {
    let requested = "";
    const client = new CatalogClient(new URL("https://gateway.example"), async (input) => {
      requested = String(input);
      return jsonResponse(catalogBody);
    });

    const result = await client.search("running shoes", new AbortController().signal);

    expect(requested).toBe(
      "https://gateway.example/ucp/shopping/catalog?q=running+shoes&limit=50",
    );
    expect(result[0]?.variants[0]).toMatchObject({
      id: "var_stride_42_black",
      unitPriceMinor: 249_900,
      availableQuantity: 4,
    });
  });

  it("fetches one exact variant without accepting a path injection", async () => {
    let requested = "";
    const client = new CatalogClient(new URL("https://gateway.example"), async (input) => {
      requested = String(input);
      return jsonResponse({
        ...catalogBody.items[0].variants[0],
        product_id: "prod_stride",
        product_name: "Stride Runner",
      });
    });

    const variant = await client.getVariant("var/unsafe", new AbortController().signal);

    expect(requested).toBe(
      "https://gateway.example/ucp/shopping/catalog/variants/var%2Funsafe",
    );
    expect(variant.productName).toBe("Stride Runner");
  });

  it.each([
    ["redirect", new Response(null, { status: 302, headers: { location: "https://evil.example" } })],
    ["wrong media type", new Response("{}", { status: 200, headers: { "content-type": "text/plain" } })],
    [
      "oversized body",
      new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json", "content-length": "262145" },
      }),
    ],
    ["server failure", jsonResponse({}, { status: 500 })],
  ])("rejects a %s response", async (_label, response) => {
    const client = new CatalogClient(new URL("https://gateway.example"), async () => response);

    await expect(client.search("shoe", new AbortController().signal)).rejects.toBeInstanceOf(
      CatalogError,
    );
  });

  it.each([
    [{ ...catalogBody, items: [{ ...catalogBody.items[0], variants: [] }] }],
    [
      {
        ...catalogBody,
        items: [
          {
            ...catalogBody.items[0],
            variants: [
              { ...catalogBody.items[0].variants[0], available_quantity: -1 },
            ],
          },
        ],
      },
    ],
    [
      {
        ...catalogBody,
        items: [
          {
            ...catalogBody.items[0],
            variants: [
              catalogBody.items[0].variants[0],
              catalogBody.items[0].variants[0],
            ],
          },
        ],
      },
    ],
  ])("rejects malformed or duplicate merchant data", async (body) => {
    const client = new CatalogClient(new URL("https://gateway.example"), async () =>
      jsonResponse(body),
    );

    await expect(client.search("shoe", new AbortController().signal)).rejects.toMatchObject({
      code: "catalog_invalid",
    });
  });
});
