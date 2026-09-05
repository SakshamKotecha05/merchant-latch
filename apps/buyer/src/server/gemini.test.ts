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
  it("uses bounded Generate Content JSON output and returns only validated intent", async () => {
    const calls: unknown[] = [];
    const client: GeminiInteractionClient = {
      create: async (request) => {
        calls.push({ request, options: undefined });
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
          contents: "Find two black running shoes under INR 5,000",
          config: expect.objectContaining({
            systemInstruction: expect.stringContaining(
              "exclude quantity words, color, size, budget, and currency",
            ),
            maxOutputTokens: 256,
            responseMimeType: "application/json",
            responseJsonSchema: expect.objectContaining({ type: "object" }),
            thinkingConfig: { thinkingLevel: "MINIMAL" },
            abortSignal: expect.any(AbortSignal),
            httpOptions: expect.objectContaining({
              timeout: 10_000,
              retryOptions: expect.objectContaining({ attempts: 3 }),
            }),
          }),
        }),
        options: undefined,
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
      create: async (request) => {
        providerSignal = request.config.abortSignal;
        return await new Promise((_, reject) => {
          request.config.abortSignal.addEventListener(
            "abort",
            () => reject(request.config.abortSignal.reason),
            {
              once: true,
            },
          );
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
