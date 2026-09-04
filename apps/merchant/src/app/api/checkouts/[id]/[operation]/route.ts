import { NextRequest, NextResponse } from "next/server";
import { checkoutRequest, settings, validCheckout } from "@/server/gateway";

type Context = { params: Promise<{ id: string; operation: string }> };

async function handle(request: NextRequest, context: Context) {
  const { id, operation } = await context.params;
  if (!validCheckout(id)) return NextResponse.json({ code: "checkout_not_found" }, { status: 404 });
  const mutation = request.method === "POST";
  if (mutation && request.headers.get("origin") !== settings().merchant) {
    return NextResponse.json({ code: "merchant_request_rejected" }, { status: 403 });
  }
  if (!(mutation ? ["approve", "confirm", "launch"] : ["review", "status"]).includes(operation)) {
    return NextResponse.json({ code: "operation_not_found" }, { status: 404 });
  }
  try {
    const raw = mutation ? await request.text() : "";
    if (raw.length > 4096) return NextResponse.json({ code: "request_too_large" }, { status: 413 });
    const body = raw ? JSON.parse(raw) : {};
    let path = `/api/checkouts/${id}/${operation}`;
    if (operation === "launch" || operation === "confirm") {
      if (typeof body.attempt_id !== "string" || !/^att_[a-zA-Z0-9_-]{1,60}$/.test(body.attempt_id)) {
        return NextResponse.json({ code: "invalid_attempt" }, { status: 400 });
      }
      path = operation === "launch" ? `/api/payments/razorpay/launch/${body.attempt_id}` : "/api/payments/razorpay/confirm";
    }
    const result = await checkoutRequest(id, path, {
      method: mutation && operation !== "launch" ? "POST" : "GET",
      ...(mutation && operation !== "launch" ? { body: raw } : {}),
      headers: operation === "approve" ? { "Idempotency-Key": `approve:${id}` } : {},
    });
    return NextResponse.json(result.body, { status: result.status, headers: { "Cache-Control": "no-store" } });
  } catch {
    return NextResponse.json({ code: "merchant_unavailable" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

export const GET = handle;
export const POST = handle;
