"use client";

import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";

import type { PurchasePlan } from "../server/planning";
import { confirmPurchase, planManualPurchase, planPurchase } from "./buyer-actions";
import {
  canSubmitCheckout,
  formatMinorAmount,
  handoffLinkAttributes,
  initialBuyerFlowState,
  parseMajorAmountToMinor,
  reduceBuyerFlow,
} from "./buyer-flow-model";
import styles from "./buyer-flow.module.css";

const examples = [
  "One black running shoe in size 42 under INR 3,000",
  "Two blue cotton shirts in size M under INR 2,500",
] as const;

const LatchMark = () => (
  <svg aria-hidden="true" viewBox="0 0 32 32" className={styles.mark}>
    <path d="M7 5v22M25 5v22M7 16h18" />
    <path d="m15 11 5 5-5 5" />
  </svg>
);

const CheckIcon = () => (
  <svg aria-hidden="true" viewBox="0 0 20 20" className={styles.icon}>
    <path d="m4 10 4 4 8-9" />
  </svg>
);

const ArrowIcon = () => (
  <svg aria-hidden="true" viewBox="0 0 20 20" className={styles.icon}>
    <path d="M4 10h12m-5-5 5 5-5 5" />
  </svg>
);

const Field = ({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) => (
  <label className={styles.field}>
    <span className={styles.fieldLabel}>{label}</span>
    {children}
    {hint ? <span className={styles.fieldHint}>{hint}</span> : null}
  </label>
);

export function BuyerFlow() {
  const [state, dispatch] = useReducer(reduceBuyerFlow, initialBuyerFlowState);
  const [manualVariant, setManualVariant] = useState("");
  const [manualQuantity, setManualQuantity] = useState("1");
  const [manualBudget, setManualBudget] = useState("");
  const [manualCurrency, setManualCurrency] = useState("INR");
  const requestSequence = useRef(0);
  const stateHeading = useRef<HTMLHeadingElement>(null);
  const errorSummary = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (state.error) errorSummary.current?.focus();
    else if (state.step !== "request") stateHeading.current?.focus();
  }, [state.error, state.step]);

  const nextRequest = (): number => {
    requestSequence.current += 1;
    return requestSequence.current;
  };

  const submitRequest = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const requestId = nextRequest();
    dispatch({ type: "operation_started", operation: "plan", requestId });
    const result = await planPurchase(state.prompt);
    dispatch(
      result.ok
        ? { type: "plan_succeeded", requestId, plan: result.data }
        : { type: "operation_failed", requestId, error: result.error },
    );
  };

  const submitManual = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const requestId = nextRequest();
    dispatch({ type: "operation_started", operation: "manual", requestId });
    const budgetMinor = manualBudget.trim()
      ? parseMajorAmountToMinor(manualBudget, manualCurrency.trim().toUpperCase())
      : undefined;
    if (budgetMinor === null) {
      dispatch({
        type: "operation_failed",
        requestId,
        error: {
          code: "invalid_input",
          message: "Check the shopping request and try again.",
          recoverable: true,
        },
      });
      return;
    }
    const result = await planManualPurchase({
      variantId: manualVariant.trim(),
      quantity: Number(manualQuantity),
      ...(budgetMinor === undefined ? {} : { budgetMinor }),
      ...(manualCurrency.trim() ? { currency: manualCurrency.trim().toUpperCase() } : {}),
    });
    dispatch(
      result.ok
        ? { type: "plan_succeeded", requestId, plan: result.data }
        : { type: "operation_failed", requestId, error: result.error },
    );
  };

  const submitConfirmation = async () => {
    if (!canSubmitCheckout(state) || !state.plan) return;
    const requestId = nextRequest();
    dispatch({ type: "operation_started", operation: "checkout", requestId });
    const result = await confirmPurchase(state.plan.confirmationToken);
    dispatch(
      result.ok
        ? { type: "checkout_succeeded", requestId, result: result.data }
        : { type: "operation_failed", requestId, error: result.error },
    );
  };

  const currentStep = state.step === "request" ? 1 : state.step === "review" ? 2 : 3;
  const pendingLabel =
    state.pending === "plan"
      ? "Finding a merchant match"
      : state.pending === "manual"
        ? "Checking that exact variant"
        : state.pending === "checkout"
          ? "Verifying the merchant handoff"
          : "";

  return (
    <div className={styles.pageShell}>
      <a className={styles.skipLink} href="#main-content">
        Skip to buying workspace
      </a>
      <header className={styles.header}>
        <a className={styles.brand} href="#main-content" aria-label="MerchantLatch home">
          <LatchMark />
          <span>MerchantLatch</span>
        </a>
        <span className={styles.demoFlag}>Buyer demo - no payment</span>
      </header>

      <nav className={styles.progress} aria-label="Purchase progress">
        {["Request", "Review terms", "Merchant handoff"].map((label, index) => {
          const position = index + 1;
          const complete = position < currentStep;
          const current = position === currentStep;
          return (
            <div
              className={`${styles.progressStep} ${complete ? styles.progressComplete : ""} ${current ? styles.progressCurrent : ""}`}
              key={label}
              aria-current={current ? "step" : undefined}
            >
              <span className={styles.progressNode}>{complete ? <CheckIcon /> : position}</span>
              <span>{label}</span>
            </div>
          );
        })}
      </nav>

      <main id="main-content" className={styles.main}>
        <section className={styles.workspace} aria-live="polite" aria-busy={Boolean(state.pending)}>
          {state.error ? (
            <div className={styles.error} role="alert" tabIndex={-1} ref={errorSummary}>
              <span className={styles.errorCode}>{state.error.code.replaceAll("_", " ")}</span>
              <strong>{state.error.message}</strong>
              {state.error.code === "checkout_outcome_unknown" ? (
                <p>Do not submit again until you check with the merchant.</p>
              ) : null}
            </div>
          ) : null}

          {state.step === "request" ? (
            <RequestWorkspace
              prompt={state.prompt}
              pending={state.pending}
              manualOpen={state.manualOpen}
              manualVariant={manualVariant}
              manualQuantity={manualQuantity}
              manualBudget={manualBudget}
              manualCurrency={manualCurrency}
              onPrompt={(prompt) => dispatch({ type: "prompt_changed", prompt })}
              onSubmit={submitRequest}
              onManualToggle={() => dispatch({ type: "manual_toggled" })}
              onManualSubmit={submitManual}
              onManualVariant={setManualVariant}
              onManualQuantity={setManualQuantity}
              onManualBudget={setManualBudget}
              onManualCurrency={setManualCurrency}
            />
          ) : state.step === "review" && state.plan ? (
            <ReviewWorkspace
              headingRef={stateHeading}
              plan={state.plan}
              acknowledged={state.acknowledged}
              pending={state.pending === "checkout"}
              onAcknowledged={(acknowledged) =>
                dispatch({ type: "acknowledgement_changed", acknowledged })
              }
              onRevise={() => dispatch({ type: "revise_request" })}
              onConfirm={submitConfirmation}
            />
          ) : state.step === "handoff" && state.result ? (
            <HandoffWorkspace
              headingRef={stateHeading}
              outcome={state.result.outcome}
              continueUrl={state.result.continueUrl}
              onStartOver={() => dispatch({ type: "start_over" })}
            />
          ) : null}

          <span className={styles.liveStatus} role="status">
            {pendingLabel}
          </span>
        </section>

        <aside className={styles.ledger} aria-labelledby="ledger-title">
          <p className={styles.eyebrow}>Safety ledger</p>
          <h2 id="ledger-title">What MerchantLatch checks</h2>
          <ul>
            <li>
              <CheckIcon />
              <span>Merchant price and stock</span>
            </li>
            <li>
              <CheckIcon />
              <span>Your exact quantity and budget</span>
            </li>
            <li>
              <CheckIcon />
              <span>Signed merchant response</span>
            </li>
          </ul>
          <p className={styles.ledgerBoundary}>
            MerchantLatch does not collect payment details. Payment happens only on the merchant
            site after you choose to continue.
          </p>
        </aside>
      </main>
    </div>
  );
}

function RequestWorkspace(props: {
  prompt: string;
  pending: "plan" | "manual" | "checkout" | null;
  manualOpen: boolean;
  manualVariant: string;
  manualQuantity: string;
  manualBudget: string;
  manualCurrency: string;
  onPrompt: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onManualToggle: () => void;
  onManualSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onManualVariant: (value: string) => void;
  onManualQuantity: (value: string) => void;
  onManualBudget: (value: string) => void;
  onManualCurrency: (value: string) => void;
}) {
  const busy = props.pending === "plan" || props.pending === "manual";
  return (
    <div className={styles.statePanel}>
      <p className={styles.eyebrow}>Make a safe request</p>
      <h1>Find the right item. Lock the right terms.</h1>
      <p className={styles.lede}>
        Describe one item, quantity, preferences, and budget. MerchantLatch uses AI only to read
        your request, then checks every fact against the merchant.
      </p>
      <form className={styles.requestForm} onSubmit={props.onSubmit}>
        <Field
          label="What are you looking for?"
          hint="Include quantity, color, size, budget, and currency when they matter."
        >
          <textarea
            name="shopping-request"
            value={props.prompt}
            onChange={(event) => props.onPrompt(event.target.value)}
            minLength={3}
            maxLength={500}
            rows={5}
            required
            disabled={busy}
            placeholder="One black running shoe in size 42 under INR 3,000"
          />
        </Field>
        <div className={styles.examples} aria-label="Example requests">
          {examples.map((example) => (
            <button type="button" key={example} onClick={() => props.onPrompt(example)} disabled={busy}>
              {example}
            </button>
          ))}
        </div>
        <button className={styles.primaryButton} type="submit" disabled={busy || props.prompt.trim().length < 3}>
          {props.pending === "plan" ? "Finding merchant match..." : "Find merchant match"}
          <ArrowIcon />
        </button>
      </form>

      <div className={styles.manualSection}>
        <button
          className={styles.textButton}
          type="button"
          aria-expanded={props.manualOpen}
          onClick={props.onManualToggle}
          disabled={busy}
        >
          {props.manualOpen ? "Hide exact variant form" : "Use exact variant instead"}
        </button>
        {props.manualOpen ? (
          <form className={styles.manualForm} onSubmit={props.onManualSubmit}>
            <fieldset disabled={busy}>
              <legend>Exact merchant variant</legend>
              <Field label="Variant ID" hint="Copy the exact variant ID from the merchant catalog.">
                <input
                  value={props.manualVariant}
                  onChange={(event) => props.onManualVariant(event.target.value)}
                  maxLength={256}
                  required
                  autoComplete="off"
                />
              </Field>
              <div className={styles.inlineFields}>
                <Field label="Quantity">
                  <input
                    type="number"
                    inputMode="numeric"
                    min="1"
                    max="20"
                    step="1"
                    value={props.manualQuantity}
                    onChange={(event) => props.onManualQuantity(event.target.value)}
                    required
                  />
                </Field>
                <Field label="Budget in major units" hint="Optional">
                  <input
                    type="number"
                    inputMode="decimal"
                    min="0.01"
                    max="1000000"
                    step="any"
                    value={props.manualBudget}
                    onChange={(event) => props.onManualBudget(event.target.value)}
                  />
                </Field>
                <Field label="Currency">
                  <input
                    value={props.manualCurrency}
                    onChange={(event) => props.onManualCurrency(event.target.value)}
                    minLength={3}
                    maxLength={3}
                    pattern="[A-Za-z]{3}"
                    autoCapitalize="characters"
                    required
                  />
                </Field>
              </div>
              <button className={styles.secondaryButton} type="submit">
                {props.pending === "manual" ? "Checking variant..." : "Check exact variant"}
              </button>
            </fieldset>
          </form>
        ) : null}
      </div>
    </div>
  );
}

export function ReviewWorkspace({
  headingRef,
  plan,
  acknowledged,
  pending,
  onAcknowledged,
  onRevise,
  onConfirm,
}: {
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  plan: PurchasePlan;
  acknowledged: boolean;
  pending: boolean;
  onAcknowledged: (value: boolean) => void;
  onRevise: () => void;
  onConfirm: () => void;
}) {
  const item = plan.recommended;
  return (
    <div className={styles.statePanel}>
      <p className={styles.eyebrow}>Current merchant terms</p>
      <h1 tabIndex={-1} ref={headingRef}>
        Review before anything leaves MerchantLatch.
      </h1>
      <p className={styles.lede}>
        MerchantLatch checked this selection against the merchant’s current catalog and your
        request.
      </p>

      <article className={styles.docket} aria-labelledby="recommended-item">
        <div className={styles.docketHeader}>
          <div>
            <span className={styles.docketLabel}>Recommended match</span>
            <h2 id="recommended-item">{item.productName}</h2>
          </div>
          <span className={styles.stock}>{item.availableQuantity} available</span>
        </div>
        <dl className={styles.facts}>
          <div>
            <dt>Variant</dt>
            <dd>{item.sku}</dd>
          </div>
          <div>
            <dt>Color / size</dt>
            <dd>
              {item.color} / {item.size}
            </dd>
          </div>
          <div>
            <dt>Quantity</dt>
            <dd>{item.quantity}</dd>
          </div>
          <div>
            <dt>Unit price</dt>
            <dd>{formatMinorAmount(item.unitPriceMinor, item.currency)}</dd>
          </div>
          <div className={styles.totalFact}>
            <dt>Merchant total</dt>
            <dd>{formatMinorAmount(item.totalMinor, item.currency)}</dd>
          </div>
        </dl>
        <p className={styles.expiry}>
          Terms expire at <time dateTime={plan.expiresAt}>{new Date(plan.expiresAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>.
        </p>
      </article>

      {plan.alternatives.length ? (
        <details className={styles.alternatives}>
          <summary>{plan.alternatives.length} other merchant matches</summary>
          <ul>
            {plan.alternatives.map((alternative) => (
              <li key={alternative.variantId}>
                <span>{alternative.productName}</span>
                <span>{alternative.color} / {alternative.size}</span>
                <strong>{formatMinorAmount(alternative.totalMinor, alternative.currency)}</strong>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <label className={styles.acknowledgement}>
        <input
          type="checkbox"
          checked={acknowledged}
          onChange={(event) => onAcknowledged(event.target.checked)}
          disabled={pending}
        />
        <span>
          <strong>I reviewed this item, price, quantity, and current stock.</strong>
          <small>This authorizes a signed checkout request, not a payment.</small>
        </span>
      </label>
      <div className={styles.reviewActions}>
        <button className={styles.textButton} type="button" onClick={onRevise} disabled={pending}>
          Revise request
        </button>
        <button
          className={styles.primaryButton}
          type="button"
          onClick={onConfirm}
          disabled={!acknowledged || pending}
        >
          {pending ? "Verifying handoff..." : "Create merchant handoff"}
          <ArrowIcon />
        </button>
      </div>
    </div>
  );
}

export function HandoffWorkspace({ headingRef, outcome, continueUrl, onStartOver }: {
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  outcome: string;
  continueUrl?: string;
  onStartOver: () => void;
}) {
  const continuation = handoffLinkAttributes(continueUrl);
  const requiresHandoff = outcome === "requires_escalation";
  return (
    <div className={styles.statePanel}>
      <div className={styles.verifiedSeal} aria-hidden="true">
        <CheckIcon />
      </div>
      <p className={styles.eyebrow}>Signed response verified</p>
      <h1 tabIndex={-1} ref={headingRef}>
        {requiresHandoff ? "Continue with the merchant." : "Merchant response verified."}
      </h1>
      <p className={styles.lede}>
        {requiresHandoff
          ? "The merchant needs you to finish this checkout on its site. No payment has been created here."
          : `The merchant returned ${outcome.replaceAll("_", " ")}. No payment was created by this buyer.`}
      </p>
      <div className={styles.handoffCard}>
        <span>Verified outcome</span>
        <strong>{outcome.replaceAll("_", " ")}</strong>
      </div>
      <div className={styles.reviewActions}>
        <button className={styles.textButton} type="button" onClick={onStartOver}>
          Start another request
        </button>
        {requiresHandoff && continuation ? (
          <a className={styles.primaryButton} {...continuation}>
            Continue to merchant
            <ArrowIcon />
          </a>
        ) : null}
      </div>
    </div>
  );
}
