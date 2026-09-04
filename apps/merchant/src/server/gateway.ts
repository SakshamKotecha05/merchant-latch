import "server-only";
import { cookies } from "next/headers";

export function settings() {
  const gateway = new URL(process.env.PUBLIC_GATEWAY_URL ?? "http://127.0.0.1:8000");
  const merchant = new URL(process.env.PUBLIC_MERCHANT_URL ?? "http://localhost:3001");
  for (const url of [gateway, merchant]) {
    const local = process.env.NODE_ENV !== "production" && ["localhost", "127.0.0.1"].includes(url.hostname);
    if ((!local && url.protocol !== "https:") || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
      throw new Error("Invalid service origin configuration");
    }
  }
  return { gateway: gateway.origin, merchant: merchant.origin };
}

export function validCheckout(id: string) {
  return /^chk_[a-zA-Z0-9_-]{1,60}$/.test(id);
}

export function cookieName(id: string) {
  if (!validCheckout(id)) throw new Error("Invalid checkout");
  return `${process.env.NODE_ENV === "production" ? "__Host-" : ""}ml_${id}`;
}

export async function gatewayRequest(path: string, options: RequestInit = {}) {
  const response = await fetch(`${settings().gateway}${path}`, {
    ...options, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(15000),
    headers: { "Content-Type": "application/json", Origin: settings().merchant, ...options.headers },
  });
  const body = await response.text();
  if (body.length > 128000) throw new Error("Gateway response too large");
  return { ok: response.ok, status: response.status, body: body ? JSON.parse(body) : {} };
}

export async function checkoutRequest(id: string, path: string, options: RequestInit = {}) {
  const value = (await cookies()).get(cookieName(id))?.value;
  const [session, csrf] = value?.split(".") ?? [];
  if (!session || !csrf) return { ok: false, status: 401, body: { code: "merchant_session_required" } };
  return gatewayRequest(path, { ...options, headers: {
    Authorization: `Bearer ${session}`, "X-CSRF-Token": csrf, ...options.headers,
  } });
}
