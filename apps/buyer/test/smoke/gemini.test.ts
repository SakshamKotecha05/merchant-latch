import { describe, expect, it } from "vitest";

import { createGeminiIntentExtractor } from "../../src/server/gemini";

const apiKey = process.env.GEMINI_API_KEY;

describe("Gemini intent smoke", () => {
  it.skipIf(!apiKey)(
    "returns exact product-only intents for both demo requests",
    async () => {
      const extractor = createGeminiIntentExtractor(
        apiKey!,
        process.env.GEMINI_MODEL ?? "gemini-3.5-flash-lite",
      );
      const stride = await extractor.extract(
        "Find one black running shoe in size 42 under INR 3000",
        new AbortController().signal,
      );
      const court = await extractor.extract(
        "One Court Low in stone, size 41, under INR 6,000",
        new AbortController().signal,
      );

      expect(stride).toMatchObject({
        quantity: 1,
        color: "black",
        size: "42",
        budgetMinor: 300_000,
        currency: "INR",
      });
      expect(court).toEqual({
        searchQuery: "Court Low",
        quantity: 1,
        color: "stone",
        size: "41",
        budgetMinor: 600_000,
        currency: "INR",
      });
    },
    15_000,
  );
});
