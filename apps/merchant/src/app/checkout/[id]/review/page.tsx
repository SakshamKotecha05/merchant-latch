import { notFound } from "next/navigation";
import { validCheckout } from "@/server/gateway";
import Checkout from "./checkout";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!validCheckout(id)) notFound();
  return <Checkout id={id} />;
}
