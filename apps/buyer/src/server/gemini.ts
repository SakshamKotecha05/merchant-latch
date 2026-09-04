import { GoogleGenAI } from "@google/genai";

import {
  IntentExtractionError,
  parseIntentInput,
  type IntentExtractor,
  type ShoppingIntent,
  ShoppingIntentSchema,
} from "./intent";

export type GeminiInteractionRequest = Readonly<{
  model: string;
  input: string;
  system_instruction: string;
  generation_config: Readonly<{ thinking_level: "low"; max_output_tokens: 256 }>;
  response_format: Readonly<{
    type: "text";
    mime_type: "application/json";
    schema: Readonly<Record<string, unknown>>;
  }>;
  store: false;
}>;

export interface GeminiInteractionClient {
  create(
    request: GeminiInteractionRequest,
    options: Readonly<{ timeout_ms: 5_000; maxRetries: 0; signal: AbortSignal }>,
  ): Promise<Readonly<{ output_text?: string }>>;
}

const intentJsonSchema = Object.freeze({
  type: "object",
  additionalProperties: false,
  properties: {
    searchQuery: {
      type: "string",
      minLength: 1,
      maxLength: 128,
      description: "Short merchant-catalog search terms only.",
    },
    quantity: { type: "integer", minimum: 1, maximum: 20 },
    color: { type: "string", minLength: 1, maxLength: 64 },
    size: { type: "string", minLength: 1, maxLength: 64 },
    budgetMinor: {
      type: "integer",
      minimum: 1,
      maximum: 100_000_000,
      description: "Total budget in the smallest currency unit when explicitly supplied.",
    },
    currency: {
      type: "string",
      pattern: "^[A-Za-z]{3}$",
      description: "Three-letter currency code when explicitly supplied.",
    },
  },
  required: ["searchQuery", "quantity"],
});

const systemInstruction = [
  "Extract shopping constraints from the user text.",
  "Preserve every explicitly stated color, size, budget, and currency.",
  "Never invent a product, variant identifier, price, inventory value, discount, payment, or action.",
  "Use budgetMinor only when the user states a total budget and convert major currency units to minor units.",
  "Return only fields defined by the response schema.",
].join(" ");

export class GeminiIntentExtractor implements IntentExtractor {
  constructor(
    private readonly client: GeminiInteractionClient,
    private readonly model: string,
  ) {}

  async extract(text: string, signal: AbortSignal): Promise<ShoppingIntent> {
    const input = parseIntentInput(text);
    let response: Readonly<{ output_text?: string }>;
    try {
      response = await this.client.create(
        {
          model: this.model,
          input,
          system_instruction: systemInstruction,
          generation_config: { thinking_level: "low", max_output_tokens: 256 },
          response_format: {
            type: "text",
            mime_type: "application/json",
            schema: intentJsonSchema,
          },
          store: false,
        },
        {
          timeout_ms: 5_000,
          maxRetries: 0,
          signal: AbortSignal.any([signal, AbortSignal.timeout(5_000)]),
        },
      );
    } catch {
      throw new IntentExtractionError("model_unavailable");
    }
    if (typeof response.output_text !== "string" || response.output_text.length > 4_096) {
      throw new IntentExtractionError("model_output_invalid");
    }
    try {
      return ShoppingIntentSchema.parse(JSON.parse(response.output_text));
    } catch {
      throw new IntentExtractionError("model_output_invalid");
    }
  }
}

export const createGeminiIntentExtractor = (
  apiKey: string,
  model: string,
): GeminiIntentExtractor => {
  const client = new GoogleGenAI({ apiKey });
  return new GeminiIntentExtractor(
    {
      create: async (request, options) => {
        const response = await client.interactions.create(request, options);
        if (Symbol.asyncIterator in response) {
          throw new IntentExtractionError("model_unavailable");
        }
        return { output_text: response.output_text };
      },
    },
    model,
  );
};
