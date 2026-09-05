import { NextRequest, NextResponse } from "next/server";
import { gatewayRequest, merchantBrowserOrigin } from "@/server/gateway";

const name = process.env.NODE_ENV === "production" ? "__Host-ml_operator" : "ml_operator";

export async function POST(request: NextRequest, context: { params: Promise<{ operation: string }> }) {
  if (request.headers.get("origin") !== merchantBrowserOrigin()) return NextResponse.json({ code: "request_rejected" }, { status: 403 });
  const { operation } = await context.params;
  if (!["login", "logout"].includes(operation)) return NextResponse.json({}, { status: 404 });
  try {
    const body = await request.text();
    if (body.length > 2048) return NextResponse.json({}, { status: 413 });
    const [session, csrf] = request.cookies.get(name)?.value.split(".") ?? [];
    const result = await gatewayRequest(`/internal/merchant/${operation}`, {
      method: "POST", ...(operation === "login" ? { body } : {}),
      headers: operation === "logout" ? { Authorization: `Bearer ${session ?? ""}`, "X-CSRF-Token": csrf ?? "" } : {},
    });
    const response = NextResponse.json({ ok: result.ok }, { status: result.status === 204 ? 200 : result.status, headers: { "Cache-Control": "no-store" } });
    if (operation === "login" && result.ok) response.cookies.set(name, `${result.body.session}.${result.body.csrf}`, { httpOnly: true, secure: process.env.NODE_ENV === "production", sameSite: "strict", path: "/", maxAge: 3600 });
    if (operation === "logout") response.cookies.set(name, "", { httpOnly: true, secure: process.env.NODE_ENV === "production", sameSite: "strict", path: "/", maxAge: 0 });
    return response;
  } catch { return NextResponse.json({ code: "operator_unavailable" }, { status: 503 }); }
}

export async function GET(request: NextRequest, context: { params: Promise<{ operation: string }> }) {
  if ((await context.params).operation !== "overview") return NextResponse.json({}, { status: 404 });
  const [session] = request.cookies.get(name)?.value.split(".") ?? [];
  if (!session) return NextResponse.json({}, { status: 401 });
  try {
    const result = await gatewayRequest("/internal/merchant/overview", { headers: { Authorization: `Bearer ${session}` } });
    return NextResponse.json(result.body, { status: result.status, headers: { "Cache-Control": "no-store" } });
  } catch { return NextResponse.json({}, { status: 503 }); }
}
