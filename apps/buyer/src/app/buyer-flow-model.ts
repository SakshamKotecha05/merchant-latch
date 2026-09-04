import type { PurchasePlan } from "../server/planning";

export type BuyerActionError = Readonly<{
  code: string;
  message: string;
  recoverable: boolean;
}>;

export type BuyerActionResult<T> =
  | Readonly<{ ok: true; data: T }>
  | Readonly<{ ok: false; error: BuyerActionError }>;

export type BuyerCheckoutView = Readonly<{
  outcome: string;
  continueUrl?: string;
}>;

export type BuyerFlowState = Readonly<{
  step: "request" | "review" | "handoff";
  prompt: string;
  manualOpen: boolean;
  plan: PurchasePlan | null;
  result: BuyerCheckoutView | null;
  pending: "plan" | "manual" | "checkout" | null;
  activeRequestId: number;
  error: BuyerActionError | null;
  acknowledged: boolean;
}>;

export type BuyerFlowEvent =
  | Readonly<{ type: "prompt_changed"; prompt: string }>
  | Readonly<{ type: "manual_toggled" }>
  | Readonly<{
      type: "operation_started";
      operation: "plan" | "manual" | "checkout";
      requestId: number;
    }>
  | Readonly<{ type: "plan_succeeded"; requestId: number; plan: PurchasePlan }>
  | Readonly<{
      type: "checkout_succeeded";
      requestId: number;
      result: BuyerCheckoutView;
    }>
  | Readonly<{ type: "operation_failed"; requestId: number; error: BuyerActionError }>
  | Readonly<{ type: "acknowledgement_changed"; acknowledged: boolean }>
  | Readonly<{ type: "revise_request" }>
  | Readonly<{ type: "start_over" }>;

export const initialBuyerFlowState: BuyerFlowState = Object.freeze({
  step: "request",
  prompt: "",
  manualOpen: false,
  plan: null,
  result: null,
  pending: null,
  activeRequestId: 0,
  error: null,
  acknowledged: false,
});

const currentRequest = (state: BuyerFlowState, requestId: number): boolean =>
  state.activeRequestId === requestId;

export const reduceBuyerFlow = (
  state: BuyerFlowState,
  event: BuyerFlowEvent,
): BuyerFlowState => {
  switch (event.type) {
    case "prompt_changed":
      return { ...state, prompt: event.prompt, error: null };
    case "manual_toggled":
      return { ...state, manualOpen: !state.manualOpen, error: null };
    case "operation_started":
      if (event.operation === "checkout" && (!state.plan || !state.acknowledged)) return state;
      return {
        ...state,
        pending: event.operation,
        activeRequestId: event.requestId,
        error: null,
      };
    case "plan_succeeded":
      if (!currentRequest(state, event.requestId) || state.pending === "checkout") return state;
      return {
        ...state,
        step: "review",
        plan: event.plan,
        result: null,
        pending: null,
        error: null,
        acknowledged: false,
      };
    case "checkout_succeeded":
      if (!currentRequest(state, event.requestId) || state.pending !== "checkout") return state;
      return { ...state, step: "handoff", result: event.result, pending: null, error: null };
    case "operation_failed":
      if (!currentRequest(state, event.requestId)) return state;
      return {
        ...state,
        pending: null,
        error: event.error,
        acknowledged: state.pending === "checkout" ? false : state.acknowledged,
        manualOpen:
          state.pending === "plan" &&
          (event.error.code === "model_unavailable" ||
            event.error.code === "model_output_invalid")
            ? true
            : state.manualOpen,
      };
    case "acknowledgement_changed":
      return state.step === "review" && state.pending === null
        ? { ...state, acknowledged: event.acknowledged, error: null }
        : state;
    case "revise_request":
      return { ...initialBuyerFlowState, prompt: state.prompt, manualOpen: state.manualOpen };
    case "start_over":
      return initialBuyerFlowState;
  }
};

const currencyFractionDigits = (currency: string, locale?: string): number =>
  new Intl.NumberFormat(locale, { style: "currency", currency }).resolvedOptions()
    .maximumFractionDigits ?? 2;

export const formatMinorAmount = (minor: number, currency: string, locale?: string): string => {
  const formatter = new Intl.NumberFormat(locale, { style: "currency", currency });
  const fractionDigits = currencyFractionDigits(currency, locale);
  return formatter.format(minor / 10 ** fractionDigits);
};

export const parseMajorAmountToMinor = (value: string, currency: string): number | null => {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return null;
  try {
    const fractionDigits = currencyFractionDigits(currency);
    const fraction = match[2] ?? "";
    if (fraction.length > fractionDigits) return null;
    const minor = BigInt(match[1]) * 10n ** BigInt(fractionDigits) +
      BigInt(fraction.padEnd(fractionDigits, "0") || "0");
    if (minor < 1n || minor > 100_000_000n) return null;
    return Number(minor);
  } catch {
    return null;
  }
};

export const safeContinuationUrl = (value: string | undefined): string | null => {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.hash) return null;
    return url.href;
  } catch {
    return null;
  }
};

export const canSubmitCheckout = (state: BuyerFlowState): boolean =>
  state.step === "review" &&
  state.plan !== null &&
  state.acknowledged &&
  state.pending === null;

export const handoffLinkAttributes = (value: string | undefined) => {
  const href = safeContinuationUrl(value);
  return href
    ? ({
        href,
        target: "_blank",
        rel: "noopener noreferrer",
        referrerPolicy: "no-referrer",
      } as const)
    : null;
};
