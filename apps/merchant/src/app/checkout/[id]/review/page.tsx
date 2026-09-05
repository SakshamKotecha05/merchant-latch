import { notFound } from "next/navigation";
import { validCheckout } from "@/server/gateway";
import Checkout from "./checkout";

export default async function Page({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ preview?: string }> }) {
  const { id } = await params;
  if (!validCheckout(id)) notFound();
  const { preview } = await searchParams;
  const previewState = process.env.NODE_ENV === "development" && (preview === "approved" || preview === "paid") ? preview : undefined;
  return <Checkout id={id} preview={previewState} />;
}
