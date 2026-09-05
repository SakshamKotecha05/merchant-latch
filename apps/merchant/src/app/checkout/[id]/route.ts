import { NextRequest, NextResponse } from "next/server";
import { cookieName, gatewayRequest, merchantBrowserOrigin, validCheckout } from "@/server/gateway";

export async function GET(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!validCheckout(id)) return new NextResponse("Checkout not found", { status: 404 });
  const target = new URL(`/checkout/${id}/review`, merchantBrowserOrigin());
  const response = NextResponse.redirect(target, 303);
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("Referrer-Policy", "no-referrer");
  const continuation = request.nextUrl.searchParams.get("session");
  const version = Number(request.nextUrl.searchParams.get("version"));
  try {
    const [existing, , existingVersion] = request.cookies.get(cookieName(id))?.value.split(".") ?? [];
    if (existing && (!continuation || existingVersion === String(version))) {
      const status = await gatewayRequest(`/api/checkouts/${id}/status`, {
        headers: { Authorization: `Bearer ${existing}` },
      });
      if (status.ok) return response;
      if (status.status !== 401) return response;
      response.cookies.delete(cookieName(id));
    }
    if (!continuation || continuation.length > 4096 || !Number.isSafeInteger(version) || version < 1) return response;
    const result = await gatewayRequest("/api/merchant/session", {
      method: "POST", body: JSON.stringify({ checkout_id: id, version, continuation }),
    });
    if (result.ok && typeof result.body.session === "string" && typeof result.body.csrf === "string") {
      response.cookies.set(cookieName(id), `${result.body.session}.${result.body.csrf}.${version}`, {
        httpOnly: true, secure: process.env.NODE_ENV === "production", sameSite: "lax", path: "/", maxAge: 3600,
      });
    }
  } catch { /* The clean review URL explains an unavailable session without leaking tokens. */ }
  return response;
}
