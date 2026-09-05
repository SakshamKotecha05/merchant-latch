import { describe, expect, it } from "vitest";

import { buyerRequestExamples } from "./buyer-flow-content";

describe("buyer request examples", () => {
  it("uses requests supported by the seeded production catalog", () => {
    expect(buyerRequestExamples).toEqual([
      "One black Stride One in size 42 under INR 6,000",
      "One stone Court Low in size 41 under INR 6,000",
    ]);
  });
});
