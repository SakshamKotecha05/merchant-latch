"use client";

import { useEffect, useState } from "react";

type Overview = {
  checkouts: { id: string; status: string; version: number; policy: number }[];
  events: { aggregate: string; type: string; source: string; at: string }[];
  exchanges: { method: string; route: string; status: number; outcome: string; request_digest: string; response_digest: string | null }[];
  queue: { pending: number; dead_lettered: number; oldest_pending_at: string | null };
};

export default function Operator() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const response = await fetch("/api/operator/overview", { cache: "no-store" });
    if (response.status === 401) { setData(null); return; }
    if (!response.ok) throw new Error("Evidence is temporarily unavailable.");
    setData(await response.json());
  }
  useEffect(() => {
    let active = true;
    void fetch("/api/operator/overview", { cache: "no-store" }).then(async response => {
      if (!response.ok) return;
      const value: Overview = await response.json();
      if (active) setData(value);
    }).catch(() => { if (active) setError("Evidence is temporarily unavailable."); });
    return () => { active = false; };
  }, []);

  async function login(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const form = event.currentTarget;
    const password = new FormData(form).get("password");
    form.reset();
    try {
      const response = await fetch("/api/operator/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
      if (!response.ok) throw new Error(response.status === 429 ? "Too many attempts. Wait one minute before trying again." : response.status === 503 ? "Operator access has not been configured or is unavailable." : "Sign-in failed. Check your password.");
      await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Sign-in unavailable."); }
    finally { setBusy(false); }
  }

  return <main id="main"><p className="eyebrow">MERCHANT OPERATIONS</p><h1>Evidence, in order.</h1><p className="intro">Inspect merchant decisions, payment recovery, and signed protocol outcomes. Raw credentials and payment payloads stay out of this view.</p>{error && <div role="alert" className="alert">{error}</div>}
    {!data ? <form className="card login" onSubmit={login}><h2>Operator sign-in</h2><label htmlFor="password">Merchant password</label><input id="password" name="password" type="password" autoComplete="current-password" required maxLength={256} /><button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button><p className="hint">Operator access is separate from buyer approval.</p></form> : <>
      <div className="toolbar"><button className="secondary" onClick={() => { setError(""); void refresh().catch(cause => setError(cause.message)); }}>Refresh evidence</button><button className="secondary" onClick={async () => { const response = await fetch("/api/operator/logout", { method: "POST" }); if (response.ok) setData(null); else setError("Sign-out could not be confirmed. Try again."); }}>Sign out</button></div>
      <section className="card"><h2>Background work</h2><dl className="facts"><div><dt>Pending jobs</dt><dd>{data.queue.pending}</dd></div><div><dt>Dead-lettered jobs</dt><dd>{data.queue.dead_lettered}</dd></div><div><dt>Oldest pending</dt><dd>{data.queue.oldest_pending_at ? new Date(data.queue.oldest_pending_at).toLocaleString() : "None"}</dd></div></dl>{data.queue.dead_lettered > 0 && <p className="expiry">Some work needs operator attention. These failures are retained, not hidden.</p>}</section>
      <section className="card"><h2>Recent checkouts</h2><div className="table-wrap"><table><thead><tr><th>Checkout</th><th>State</th><th>Version</th><th>Policy</th></tr></thead><tbody>{data.checkouts.map(row => <tr key={row.id}><td><code>{row.id}</code></td><td>{row.status.replaceAll("_", " ")}</td><td>{row.version}</td><td>{row.policy}</td></tr>)}</tbody></table></div>{!data.checkouts.length && <p>No checkouts recorded.</p>}</section>
      <section className="card"><h2>Decision and recovery timeline</h2><ul className="events">{data.events.map((row,index) => <li key={index}><strong>{row.type.replaceAll("_", " ").replaceAll(".", " · ")}</strong><small>{row.aggregate} · {row.source} · {new Date(row.at).toLocaleString()}</small></li>)}</ul>{!data.events.length && <p>No merchant events recorded.</p>}</section>
      <section className="card"><h2>UCP protocol inspector</h2><p>Digests identify recorded content. They do not independently verify an omitted raw message.</p>{data.exchanges.map((row,index) => <details key={index}><summary>{row.method} · {row.status} · {row.outcome.replaceAll("_", " ")}</summary><p>{row.route}</p><small>Request SHA-256: {row.request_digest}</small><small>Response SHA-256: {row.response_digest ?? "No accepted response"}</small></details>)}{!data.exchanges.length && <p>No protocol exchanges recorded.</p>}</section>
      <section className="card"><h2>Evaluation status</h2><p>The held-out evaluation has not been frozen or run for this revision. No accuracy or safety score is claimed.</p></section>
    </>}
  </main>;
}
