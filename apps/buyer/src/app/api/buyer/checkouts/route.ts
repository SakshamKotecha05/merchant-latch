import { z } from "zod";

import type { BuyerApplication } from "../../../../server/application";
import { apiErrorResponse, apiResponse, parseBody, readApiJson } from "../../../../server/api";
import { getBuyerApplication } from "../../../../server/runtime";

const schema = z
  .object({
    confirmationToken: z.string().min(1).max(4_096),
    confirmed: z.boolean(),
  })
  .strict();

export const createCheckoutPost =
  (application: BuyerApplication) =>
  async (request: Request): Promise<Response> => {
    try {
      const input = parseBody(schema, await readApiJson(request));
      return apiResponse(await application.createCheckout(input, request.signal), 201);
    } catch (error) {
      return apiErrorResponse(error);
    }
  };

export const POST = (request: Request): Promise<Response> =>
  createCheckoutPost(getBuyerApplication())(request);
