import { z } from "zod";

import type { BuyerApplication } from "../../../../server/application";
import { apiErrorResponse, apiResponse, parseBody, readApiJson } from "../../../../server/api";
import { getBuyerApplication } from "../../../../server/runtime";

const schema = z.object({ text: z.string() }).strict();

export const createPlanPost =
  (application: BuyerApplication) =>
  async (request: Request): Promise<Response> => {
    try {
      const input = parseBody(schema, await readApiJson(request));
      return apiResponse(await application.plan(input, request.signal));
    } catch (error) {
      return apiErrorResponse(error);
    }
  };

export const POST = (request: Request): Promise<Response> =>
  createPlanPost(getBuyerApplication())(request);
