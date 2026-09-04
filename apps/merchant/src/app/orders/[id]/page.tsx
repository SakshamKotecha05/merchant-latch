import { notFound } from "next/navigation";
import { gatewayRequest } from "@/server/gateway";

export default async function Order({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)) notFound();
  const result = await gatewayRequest(`/api/orders/${id}`).catch(() => null);
  if (!result) return <main id="main"><h1>Confirmation unavailable.</h1><p>Check again shortly. Do not create another payment.</p></main>;
  if (result.status === 404) notFound();
  if (!result.ok) return <main id="main"><h1>Confirmation unavailable.</h1><p>Check again shortly.</p></main>;
  return <main id="main"><p className="eyebrow">MERCHANT CONFIRMATION</p><h1>Payment verified.</h1><section className="card"><h2>Your merchant order is confirmed.</h2><p>Keep this private confirmation link for your records.</p><small>Order {id}</small><div className="total"><span>Paid</span><strong>{new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(result.body.amount / 100)}</strong></div><p>Razorpay Test Mode. No live money moved.</p></section></main>;
}
