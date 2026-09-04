import { loadBuyerConfig } from "../../../server/config";
import { exportPublicJwk, loadP256PrivateKey } from "../../../server/ucp/keys";

export const dynamic = "force-dynamic";

export const GET = (): Response => {
  const config = loadBuyerConfig(process.env);
  const privateKey = loadP256PrivateKey(config.buyerPrivateKeyPem);
  const profile = {
    ucp: {
      version: "2026-04-08",
      services: {},
      capabilities: {},
      payment_handlers: {},
    },
    signing_keys: [exportPublicJwk(privateKey, config.buyerKeyId)],
  };
  return new Response(JSON.stringify(profile), {
    status: 200,
    headers: {
      "Cache-Control": "public, max-age=300",
      "Content-Type": "application/json",
    },
  });
};
