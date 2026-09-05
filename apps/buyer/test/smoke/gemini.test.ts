import { describe, expect, it } from "vitest";

import { createGeminiIntentExtractor } from "../../src/server/gemini";

const apiKey = process.env.GEMINI_API_KEY;

describe("Gemini intent smoke", () => {
  it.skipIf(!apiKey)(
    "returns one schema-valid shopping intent",
    async () => {
      const extractor = createGeminiIntentExtractor(
        apiKey!,
        process.env.GEMINI_MODEL ?? "gemini-3.8-flash",
      );
      const intent = await extractor.extract(
        "Find one black running shoe in size 42 under INR 3000",
        new AbortController().signal,
      );

      expect(intent.quantity).toBe(1);
      expect(intent.searchQuery.length).toBeGreaterThan(0);
    },
    15_000,
  );
});
