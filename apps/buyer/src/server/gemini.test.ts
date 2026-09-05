import { describe, expect, it } from "vitest";

import { GeminiIntentExtractor, type GeminiInteractionClient } from "./gemini";
import { IntentExtractionError } from "./intent";

const output = JSON.stringify({
  searchQuery: "running shoes",
  quantity: 2,
  color: "black",
  budgetMinor: 500_000,
  currency: "INR",
});

describe("GeminiIntentExtractor", () => {
  it("uses structured output and returns only validated intent", async () => {
    const calls: unknown[] = [];
    const client: GeminiInteractionClient = {
      create: async (request, options) => {
        calls.push({ request, options });
        return { output_text: output };
      },
    };
    const extractor = new GeminiIntentExtractor(client, "gemini-3.8-flash");

    await expect(
      extractor.extract("Find two black running shoes under INR 5,000", new AbortController().signal),
    ).resolves.toMatchObject({ searchQuery: "running shoes", quantity: 2 });
    expect(calls).toEqual([
      {
        request: expect.objectContaining({
          model: "gemini-3.8-flash",
          input: "Find two black running shoes under INR 5,000",
          system_instruction: expect.stringContaining(
            "Preserve every explicitly stated color, size, budget, and currency.",
          ),
          generation_config: { thinking_level: "low", max_output_tokens: 256 },
          response_format: expect.objectContaining({
            type: "text",
            mime_type: "application/json",
            schema: expect.objectContaining({ type: "object" }),
          }),
          store: false,
        }),
        options: expect.objectContaining({
          timeout_ms: 10_000,
          maxRetries: 2,
          signal: expect.any(AbortSignal),
        }),
      },
    ]);
    expect(JSON.stringify(calls)).not.toContain("api-key-sentinel");
  });

  it.each([
    ["missing output", {}],
    ["invalid JSON", { output_text: "not json" }],
    ["invalid semantics", { output_text: '{"searchQuery":"shoe","quantity":1000}' }],
    ["extra action", { output_text: '{"searchQuery":"shoe","quantity":1,"pay":true}' }],
  ])("maps %s to a safe model-output error", async (_label, response) => {
    const client: GeminiInteractionClient = { create: async () => response };
    const extractor = new GeminiIntentExtractor(client, "gemini-3.8-flash");

    await expect(
      extractor.extract("find one shoe", new AbortController().signal),
    ).rejects.toMatchObject({ code: "model_output_invalid" });
  });

  it("maps provider text and timeout failures without reflecting them", async () => {
    const sentinel = "private provider body sentinel";
    const client: GeminiInteractionClient = {
      create: async () => {
        throw new Error(sentinel);
      },
    };
    const extractor = new GeminiIntentExtractor(client, "gemini-3.8-flash");

    try {
      await extractor.extract("find one shoe", new AbortController().signal);
      throw new Error("expected extraction failure");
    } catch (error) {
      expect(error).toBeInstanceOf(IntentExtractionError);
      expect(error).toMatchObject({ code: "model_unavailable" });
      expect(String(error)).not.toContain(sentinel);
    }
  });

  it("forwards caller cancellation into the bounded provider request", async () => {
    const controller = new AbortController();
    let providerSignal: AbortSignal | undefined;
    const client: GeminiInteractionClient = {
      create: async (_request, options) => {
        providerSignal = options.signal;
        return await new Promise((_, reject) => {
          options.signal.addEventListener("abort", () => reject(options.signal.reason), {
            once: true,
          });
        });
      },
    };
    const extractor = new GeminiIntentExtractor(client, "gemini-3.8-flash");

    const extraction = extractor.extract("find one shoe", controller.signal);
    controller.abort(new Error("buyer request ended"));

    await expect(extraction).rejects.toMatchObject({ code: "model_unavailable" });
    expect(providerSignal?.aborted).toBe(true);
  });

  it("rejects invalid user input before calling Gemini", async () => {
    let called = false;
    const client: GeminiInteractionClient = {
      create: async () => {
        called = true;
        return { output_text: output };
      },
    };
    const extractor = new GeminiIntentExtractor(client, "gemini-3.8-flash");

    await expect(extractor.extract("no", new AbortController().signal)).rejects.toMatchObject({
      code: "invalid_input",
    });
    expect(called).toBe(false);
  });
});
