import type { Metadata } from "next";
import Link from "next/link";
import "./merchant.css";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "MerchantLatch | Merchant checkout", description: "Review exact merchant terms before a Razorpay test payment.", robots: { index: false, follow: false }, referrer: "no-referrer" };

export default function Layout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body><a className="skip" href="#main">Skip to checkout</a><header><Link href="/" className="brand">MerchantLatch<span>MERCHANT CHECKOUT</span></Link><span className="badge">Razorpay Test Mode</span></header>{children}<footer>Merchant-controlled terms. Human-approved payment.<span>No live money moves in this demo.</span></footer></body></html>;
}
