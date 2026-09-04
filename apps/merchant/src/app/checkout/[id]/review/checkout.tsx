"use client";

import { useEffect, useRef, useState } from "react";

type Snapshot = { lines: { name: string; size: string; color: string; quantity: number; unitPriceMinor: number; sku: string }[]; pricing: { totalMinor: number; currency: string }; policyPackVersion: number };
type Review = { snapshot: Snapshot; snapshot_checksum: string; expires_at: string };
type Status = { status: string; attempt: { id: string; state: string } | null; pickup: { name: string; city: string } | null; lease_expires_at: string | null; order: { id: string; amount: number; currency: string } | null; events: { type: string; source: string; at: string }[] };
type PaymentResult = { razorpay_payment_id: string; razorpay_signature: string };
type RazorpayConstructor = new (options: Record<string, unknown>) => { open(): void; on(event: string, callback: () => void): void };
declare global { interface Window { Razorpay?: RazorpayConstructor } }

const activityLabels: Record<string, string> = {
  "checkout.created": "Purchase proposed", "checkout.updated": "Purchase details updated",
  "checkout.approved": "Purchase approved", "checkout.completed": "Order confirmed",
  "checkout.canceled": "Checkout canceled", "payment_attempt.provider_order_creating": "Payment preparation started",
  "payment_attempt.provider_order_created": "Payment ready", "payment_attempt.paid": "Payment verified",
  "payment_attempt.paid_inventory_exception": "Payment needs stock recovery",
};

const money = (value: number) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(value / 100);
const messages: Record<string, string> = {
  merchant_session_required: "This private checkout link is unavailable or expired. Return to the buyer for a new checkout.",
  approval_expired: "Your review window has expired. Return to the buyer for fresh merchant terms.",
  inventory_version_changed: "Stock changed while you were reviewing. No payment was created. Return to the buyer to check availability.",
  price_changed: "The price changed. No payment was created. Return to the buyer to review the new price.",
  checkout_version_conflict: "This checkout changed. Refresh its status before taking another action.",
  snapshot_checksum_changed: "The purchase terms changed. Return to the buyer for a fresh review.",
};

let checkoutScript: Promise<void> | undefined;
function loadCheckout() {
  if (window.Razorpay) return Promise.resolve();
  checkoutScript ??= new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://checkout.razorpay.com/v1/checkout.js";
    const fail = () => { clearTimeout(timer); script.remove(); checkoutScript = undefined; reject(new Error("Razorpay could not load. Check your connection and try again.")); };
    const timer = setTimeout(fail, 15000);
    script.onload = () => { clearTimeout(timer); resolve(); };
    script.onerror = fail;
    document.head.appendChild(script);
  });
  return checkoutScript;
}

export default function Checkout({ id }: { id: string }) {
  const [review, setReview] = useState<Review | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [clock, setClock] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);

  async function api(operation: string, body?: object) {
    const response = await fetch(`/api/checkouts/${id}/${operation}`, { method: body ? "POST" : "GET", cache: "no-store", headers: { "Content-Type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}) });
    const data = await response.json();
    if (!response.ok) {
      const code = typeof data.code === "string" ? data.code : typeof data.detail === "string" ? data.detail : "";
      throw new Error(messages[code] ?? "We could not verify this action. Refresh status before trying again; do not start a second payment.");
    }
    return data;
  }

  async function refresh() {
    const state: Status = await api("status");
    setStatus(state);
    return state;
  }

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const state: Status = await api("status");
        if (!active) return;
        setClock(Date.now());
        setStatus(state);
        if (!state.attempt && state.status === "requires_buyer_review") {
          const value: Review = await api("review");
          if (active) setReview(value);
        }
      } catch (cause) { if (active) setError(cause instanceof Error ? cause.message : "Checkout unavailable."); }
    }
    void load();
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => { active = false; clearInterval(timer); };
    // The checkout id fixes the lifetime of this view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const attemptState = status?.attempt?.state;
  useEffect(() => {
    if (!attemptState || ["paid", "refunded", "manual_review", "expired", "canceled", "failed"].includes(attemptState)) return;
    const timer = setInterval(() => { void refresh().catch(() => setNotice("Status updates paused. Use Refresh status to check again.")); }, 3000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attemptState, id]);

  const expired = !!review && Date.parse(review.expires_at) <= clock;
  const leaseActive = !!status?.lease_expires_at && Date.parse(status.lease_expires_at) > clock;
  const paid = status?.status === "completed" && !!status.order;
  const title = paid ? "Payment verified." : status?.attempt ? "Your payment status." : "Review your purchase.";

  async function approve() {
    if (!review || !confirmed || busy || expired) return;
    setBusy(true); setError("");
    try { await api("approve", { snapshot_checksum: review.snapshot_checksum, confirmed: true }); await refresh(); heading.current?.focus(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Approval unavailable."); await refresh().catch(() => undefined); }
    finally { setBusy(false); }
  }

  async function pay() {
    if (!status?.attempt || busy) return;
    const attemptId = status.attempt.id;
    setBusy(true); setError("");
    try {
      await loadCheckout();
      const config = await api("launch", { attempt_id: attemptId });
      if (!window.Razorpay) throw new Error("Razorpay Checkout is unavailable.");
      const checkout = new window.Razorpay({ key: config.key_id, order_id: config.order_id, amount: config.amount, currency: config.currency, name: "MerchantLatch", description: "Approved sneaker purchase - Test Mode", retry: { enabled: false },
        handler: async (result: PaymentResult) => {
          try { await api("confirm", { attempt_id: attemptId, razorpay_payment_id: result.razorpay_payment_id, razorpay_signature: result.razorpay_signature }); await refresh(); }
          catch { setNotice("Your payment result is being verified. Do not pay again; refresh status while the webhook recovers it."); }
          finally { setBusy(false); heading.current?.focus(); }
        }, modal: { ondismiss: () => { setBusy(false); setNotice("Checkout closed. Payment is not confirmed until merchant verification completes."); void refresh().catch(() => setNotice("Status updates paused. Use Refresh status to check again.")); } },
      });
      checkout.on("payment.failed", () => { setNotice("Razorpay reported a failed attempt. Refresh status before trying again."); setBusy(false); });
      checkout.open();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Payment unavailable."); setBusy(false); }
  }

  return <main id="main"><nav className="rail" aria-label="Checkout progress"><span className="active">01 · Merchant review</span><span className={status?.attempt ? "active" : ""}>02 · You approve</span><span className={paid ? "active" : ""}>03 · Payment verified</span></nav><p className="eyebrow">{paid ? "MERCHANT CONFIRMATION" : "EXACT TERMS. EXPLICIT CONSENT."}</p><h1 ref={heading} tabIndex={-1}>{title}</h1><p className="intro">{paid ? "Razorpay capture and your merchant order are confirmed." : "The merchant checks every purchase detail. You decide whether to continue."}</p>
    {error && <div className="alert" role="alert">{error}</div>}{notice && <div className="notice" role="status">{notice}</div>}
    <div className="columns"><section className="card">
      {!status && !error && <p role="status">Verifying your private checkout…</p>}
      {review && !status?.attempt && <><div className="section-heading"><h2>Purchase details</h2><span className="badge">Merchant verified</span></div>{review.snapshot.lines.map(line => <article className="line-item" key={line.sku}><div><h3>{line.name}</h3><p>{line.color} · Size {line.size} · Quantity {line.quantity}</p><small>{line.sku}</small></div><strong>{money(line.unitPriceMinor)}<small>each</small></strong></article>)}<dl className="facts"><div><dt>Store pickup</dt><dd>{status?.pickup?.name ?? "Merchant pickup"}<small>{status?.pickup?.city}</small></dd></div><div><dt>Pickup charge</dt><dd>Free</dd></div><div><dt>Tax</dt><dd>Included in price</dd></div></dl><div className="total"><span>Total</span><strong>{money(review.snapshot.pricing.totalMinor)}</strong></div><p className="expiry">{expired ? "Review expired. Start a new checkout in the buyer." : `Review expires in ${Math.max(0, Math.ceil((Date.parse(review.expires_at) - clock) / 1000))} seconds`}</p><label className="consent"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={expired || busy} /><span>I approve these exact items, total, and pickup location.</span></label><button onClick={approve} disabled={!confirmed || expired || busy}>{busy ? "Verifying approval…" : "Approve purchase"}</button><p className="hint">Approval reserves stock. Payment opens only on your next explicit action.</p></>}
      {status?.attempt && <><span className="eyebrow">{paid ? "CONFIRMED" : "MERCHANT STATUS"}</span><h2>{paid ? "Order confirmed for store pickup" : ({draft: "Preparing your payment", provider_order_creating: "Creating your payment order", awaiting_payment: leaseActive ? "Ready when you are" : "Payment window expired", verifying: "Verifying payment", reconciling: "Checking an uncertain result", paid_inventory_exception: "Payment received after stock release", refund_pending: "Refund in progress", refunded: "Refund confirmed", manual_review: "Merchant review needed", expired: "Payment window expired", canceled: "Checkout canceled", failed: "Payment could not proceed"} as Record<string,string>)[attemptState ?? ""] ?? "Checking payment status"}</h2><p>{paid ? `Order ${status.order?.id}` : "We never treat a browser message alone as proof of payment."}</p>{paid && <div className="total"><span>Paid</span><strong>{money(status.order!.amount)}</strong></div>}{["paid_inventory_exception", "refund_pending", "refunded"].includes(attemptState ?? "") && <p>The inventory reservation ended before payment verification. The merchant is following its full-refund policy. Do not pay again.</p>}{attemptState === "awaiting_payment" && leaseActive && <button disabled={busy} onClick={pay}>{busy ? "Checkout open…" : "Open Razorpay test checkout"}</button>}<button className="secondary" onClick={() => { setError(""); void refresh().catch(cause => setError(cause.message)); }}>Refresh status</button></>}
      {status && !status.attempt && !review && !error && <p>This checkout is {status.status.replaceAll("_", " ")}. Return to the buyer for a new purchase.</p>}
    </section><aside className="card ledger"><p className="eyebrow">YOUR SAFETY RECORD</p><h2>What happens here</h2><ol><li>Merchant prices and stock are authoritative.</li><li>Your approval binds the exact purchase.</li><li>Razorpay handles payment details.</li><li>The merchant verifies capture before fulfillment.</li></ol><hr /><h3>Checkout activity</h3>{status?.events.length ? <ul className="events">{status.events.map((event, index) => <li key={`${event.at}-${index}`}><strong>{activityLabels[event.type] ?? "Merchant status updated"}</strong><small> {new Date(event.at).toLocaleTimeString()}</small></li>)}</ul> : <p>Verified activity will appear here as the checkout progresses.</p>}</aside></div></main>;
}
