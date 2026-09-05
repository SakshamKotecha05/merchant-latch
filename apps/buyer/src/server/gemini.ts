import { GoogleGenAI, ThinkingLevel } from "@google/genai";

import {
  IntentExtractionError,
  parseIntentInput,
  type IntentExtractor,
  type ShoppingIntent,
  ShoppingIntentSchema,
} from "./intent";

export type GeminiInteractionRequest = Readonly<{
  model: string;
  contents: string;
  config: Readonly<{
    systemInstruction: string;
    temperature: 0;
    maxOutputTokens: 256;
    responseMimeType: "application/json";
    responseJsonSchema: Readonly<Record<string, unknown>>;
    thinkingConfig: Readonly<{ thinkingLevel: ThinkingLevel.MINIMAL }>;
    abortSignal: AbortSignal;
    httpOptions: Readonly<{
      timeout: 10_000;
      retryOptions: Readonly<{
        attempts: 3;
        initialDelay: 0.25;
        maxDelay: 1;
        expBase: 2;
        jitter: 0.2;
      }>;
    }>;
  }>;
}>;

export interface GeminiInteractionClient {
  create(request: GeminiInteractionRequest): Promise<Readonly<{ output_text?: string }>>;
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
          contents: input,
          config: {
            systemInstruction,
            temperature: 0,
            maxOutputTokens: 256,
            responseMimeType: "application/json",
            responseJsonSchema: intentJsonSchema,
            thinkingConfig: { thinkingLevel: ThinkingLevel.MINIMAL },
            abortSignal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]),
            httpOptions: {
              timeout: 10_000,
              retryOptions: {
                attempts: 3,
                initialDelay: 0.25,
                maxDelay: 1,
                expBase: 2,
                jitter: 0.2,
              },
            },
          },
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
      create: async (request) => {
        const response = await client.models.generateContent(request);
        return { output_text: response.text };
      },
    },
    model,
  );
};
