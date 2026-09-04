import { z } from "zod";

import type { BuyerApplication } from "../../../../../server/application";
import {
  apiErrorResponse,
  apiResponse,
  parseBody,
  readApiJson,
} from "../../../../../server/api";
import { getBuyerApplication } from "../../../../../server/runtime";

const schema = z
  .object({
    variantId: z.string().min(1).max(256),
    quantity: z.number().int().min(1).max(20),
    budgetMinor: z.number().int().min(1).max(100_000_000).optional(),
    currency: z.string().regex(/^[A-Z]{3}$/).optional(),
  })
  .strict();

export const createManualPlanPost =
  (application: BuyerApplication) =>
  async (request: Request): Promise<Response> => {
    try {
      const input = parseBody(schema, await readApiJson(request));
      return apiResponse(await application.planManual(input, request.signal));
    } catch (error) {
      return apiErrorResponse(error);
    }
  };

export const POST = (request: Request): Promise<Response> =>
  createManualPlanPost(getBuyerApplication())(request);
