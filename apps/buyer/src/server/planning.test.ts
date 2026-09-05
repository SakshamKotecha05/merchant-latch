import { describe, expect, it } from "vitest";

import type { CatalogItem, CatalogReader, CatalogVariant } from "./catalog";
import { BuyerPlanner, BuyerPlanningError, rankCandidates } from "./planning";

const black42: CatalogVariant = {
  id: "var_black_42",
  sku: "RUN-BLK-42",
  size: "42",
  color: "Black",
  unitPriceMinor: 249_900,
  currency: "INR",
  availableQuantity: 3,
  inventoryVersion: 5,
};

const items: CatalogItem[] = [
  {
    id: "prod_runner",
    name: "Stride Running Shoe",
    description: "Road running shoe",
    variants: [
      black42,
      { ...black42, id: "var_blue_42", sku: "RUN-BLU-42", color: "Blue" },
      { ...black42, id: "var_black_43", sku: "RUN-BLK-43", size: "43" },
      { ...black42, id: "var_sold_out", availableQuantity: 0 },
    ],
  },
];

describe("rankCandidates", () => {
  it("applies merchant facts and user constraints before deterministic ranking", () => {
    const result = rankCandidates(
      {
        searchQuery: "running shoe",
        quantity: 2,
        color: "black",
        size: "42",
        budgetMinor: 500_000,
        currency: "INR",
      },
      items,
    );

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({ variantId: "var_black_42", totalMinor: 499_800 });
  });

  it("never includes unavailable, unaffordable, or unsafe integer results", () => {
    const unsafe: CatalogItem[] = [
      {
        ...items[0],
        variants: [{ ...black42, id: "var_huge", unitPriceMinor: Number.MAX_SAFE_INTEGER }],
      },
    ];

    expect(rankCandidates({ searchQuery: "shoe", quantity: 2, budgetMinor: 100 }, items)).toEqual(
      [],
    );
    expect(rankCandidates({ searchQuery: "shoe", quantity: 2 }, unsafe)).toEqual([]);
  });

  it("breaks equivalent ties by total then stable variant ID", () => {
    const result = rankCandidates({ searchQuery: "running shoe", quantity: 1 }, items);

    expect(result.map((candidate) => candidate.variantId)).toEqual([
      "var_black_42",
      "var_black_43",
      "var_blue_42",
    ]);
  });
});

describe("BuyerPlanner", () => {
  const catalog = (variant: CatalogVariant = black42): CatalogReader => ({
    search: async () => items,
    getVariant: async () => ({
      ...variant,
      productId: "prod_runner",
      productName: "Stride Running Shoe",
    }),
  });

  it("creates a five-minute confirmation bound to merchant-owned terms", async () => {
    const planner = new BuyerPlanner({
      catalog: catalog(),
      sessionSecret: "s".repeat(32),
      merchantOrigin: "https://gateway.example",
      requestId: () => "550e8400-e29b-41d4-a716-446655440000",
    });

    const plan = await planner.plan(
      { searchQuery: "running shoe", quantity: 2, color: "black", size: "42" },
      new Date("2026-09-04T12:00:00Z"),
      new AbortController().signal,
    );

    expect(plan.recommended).toMatchObject({
      productName: "Stride Running Shoe",
      variantId: "var_black_42",
      quantity: 2,
      totalMinor: 499_800,
      currency: "INR",
    });
    expect(plan.expiresAt).toBe("2026-09-04T12:05:00.000Z");
    expect(plan.confirmationToken).not.toContain("Stride Running Shoe");
  });

  it("returns no recommendation when model constraints cannot pass merchant facts", async () => {
    const planner = new BuyerPlanner({
      catalog: catalog(),
      sessionSecret: "s".repeat(32),
      merchantOrigin: "https://gateway.example",
    });

    await expect(
      planner.plan(
        { searchQuery: "shoe", quantity: 20 },
        new Date("2026-09-04T12:00:00Z"),
        new AbortController().signal,
      ),
    ).rejects.toBeInstanceOf(BuyerPlanningError);
  });

  it("uses the same merchant validation for the manual fallback", async () => {
    const planner = new BuyerPlanner({
      catalog: catalog(),
      sessionSecret: "s".repeat(32),
      merchantOrigin: "https://gateway.example",
      requestId: () => "550e8400-e29b-41d4-a716-446655440000",
    });

    const plan = await planner.planManual(
      { variantId: "var_black_42", quantity: 2, budgetMinor: 500_000, currency: "INR" },
      new Date("2026-09-04T12:00:00Z"),
      new AbortController().signal,
    );
    expect(plan.recommended.totalMinor).toBe(499_800);

    await expect(
      planner.planManual(
        { variantId: "var_black_42", quantity: 4 },
        new Date("2026-09-04T12:00:00Z"),
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ code: "inventory_unavailable" });
  });
});
