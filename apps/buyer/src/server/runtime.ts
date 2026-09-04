import { createBuyerApplication, type BuyerApplication } from "./application";
import { CatalogClient } from "./catalog";
import { loadBuyerConfig } from "./config";
import { createGeminiIntentExtractor } from "./gemini";
import { BuyerPlanner } from "./planning";
import { UcpCheckoutClient } from "./ucp/client";

let application: BuyerApplication | undefined;

export const getBuyerApplication = (): BuyerApplication => {
  if (application !== undefined) return application;
  const config = loadBuyerConfig(process.env);
  const catalog = new CatalogClient(config.publicGatewayUrl);
  application = createBuyerApplication({
    extractor: createGeminiIntentExtractor(config.geminiApiKey, config.geminiModel),
    planner: new BuyerPlanner({
      catalog,
      sessionSecret: config.sessionSecret,
      merchantOrigin: config.publicGatewayUrl.origin,
    }),
    catalog,
    checkout: new UcpCheckoutClient(config),
    sessionSecret: config.sessionSecret,
    merchantOrigin: config.publicGatewayUrl.origin,
  });
  return application;
};
