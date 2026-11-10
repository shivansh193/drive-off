import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, dur, fmtTime, money, STATE_LABEL } from "../api";

const CONSENT = [
  "A camera at the lot entrance and exit reads your licence plate.",
  "We keep one entry and one exit frame per stay (faces blurred) for 30 days so you can dispute a charge, then delete them.",
  "Plates that aren't enrolled are never stored: only a one-way hash is kept, to count free spaces.",
  "When you leave, PayPal charges your linked account at the lot's posted rate. No surge pricing, no fees.",
  "You can delete your account and evidence any time from this screen.",
];

function Receipt({ r, t, onChange }: { r: any; t: string; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [exitTime, setExitTime] = useState("");
  const [err, setErr] = useState("");
  const v = r.dispute_json;
  async function dispute(reason: "not_me" | "wrong_time") {
    setBusy(true); setErr("");
    try { await api(`/api/sessions/${r.id}/dispute`, { body: { t, reason } }); onChange(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  async function confirm(useDefault: boolean) {
    setBusy(true); setErr("");
    try {
      const exit_at = useDefault || !exitTime ? null : new Date(exitTime).toISOString();
      await api(`/api/sessions/${r.id}/confirm-exit`, { body: { t, exit_at } }); onChange();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  return (
    <div className="receipt">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="lot">{r.lot.name}</span>
        <span className={`state s-${r.state}`}>{STATE_LABEL[r.state] ?? r.state}</span>
      </div>
      <div className="row" style={{ alignItems: "baseline", gap: 8, margin: "8px 0 2px" }}>
        <span className="amt">{r.state === "active" ? "Parked" : money(r.amount_cents)}</span>
        {r.refund_cents ? <span className="chip" style={{ background: "#e3eeff", color: "#1d3c8f" }}>−{money(r.refund_cents)} refunded</span> : null}
      </div>
      <div className="meta">{r.lot.rate_sign}</div>
      <dl className="lines">
        <dt>In</dt><dd>{fmtTime(r.entry_at)}</dd>
        <dt>Out</dt><dd>{fmtTime(r.exit_at)}</dd>
        <dt>Stay</dt><dd>{dur(r.entry_at, r.exit_at)}</dd>
        {r.quote_json && <><dt>How</dt><dd style={{ textAlign: "right", fontFamily: "var(--sans)" }}>{r.quote_json.explanation}</dd></>}
        {r.entry && <><dt>Read</dt><dd>{r.entry.agree}/{r.entry.frames} frames · {r.entry.confidence?.toFixed(2)}</dd></>}
      </dl>
      {(r.entry?.evidence?.frame || r.exit?.evidence?.frame) && (
        <div className="evidence">
          <figure>{r.entry?.evidence?.annotated ? <img src={r.entry.evidence.annotated} alt="entry evidence" /> : <img alt="" />}<figcaption>ENTRY · {fmtTime(r.entry?.at)}</figcaption></figure>
          <figure>{r.exit?.evidence?.annotated ? <img src={r.exit.evidence.annotated} alt="exit evidence" /> : <div style={{ aspectRatio: "16/9", background: "#eee", borderRadius: 6, display: "grid", placeItems: "center", fontSize: 11, color: "#888" }}>no exit read</div>}<figcaption>EXIT · {fmtTime(r.exit?.at)}</figcaption></figure>
        </div>
      )}
      {r.capture_id && (
        <div className="pp">
          <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden><path fill="#1d3c8f" d="M7 21H3.5L6.5 3h7c3.6 0 5.6 2 5 5.3-.6 3.5-3.2 5.2-6.6 5.2H9.6z" /></svg>
          <div>PayPal {r.paypal.mode === "mock" ? "(mock)" : "sandbox"} capture <b>{r.capture_id}</b>{r.refund_id ? <> · refund <b>{r.refund_id}</b></> : null}</div>
        </div>
      )}
      {r.state === "pending_confirm" && (
        <div className="verdict-box wait">
          <b>We didn't catch your exit.</b> Tell us when you left. You'll never pay more than the {money(r.amount_cents)} default.
          <div className="row" style={{ marginTop: 8 }}>
            <input type="datetime-local" value={exitTime} onChange={(e) => setExitTime(e.target.value)} />
            <button className="btn primary" disabled={busy || !exitTime} onClick={() => confirm(false)}>Confirm time</button>
            <button className="btn" disabled={busy} onClick={() => confirm(true)}>Accept {money(r.amount_cents)}</button>
          </div>
        </div>
      )}
      {v && (
        <div className={`verdict-box ${v.refund_cents > 0 ? "good" : "wait"}`}>
          <b>{v.outcome === "to_operator" ? "Sent to the lot operator" : v.outcome === "full_refund" ? "Refunded in full" : "Partly refunded"}</b>
          <div style={{ marginTop: 4 }}>{v.explanation}</div>
          {v.signals?.vision?.summary && <div style={{ marginTop: 6, fontSize: 12 }}><b>AI evidence review:</b> {v.signals.vision.summary}</div>}
          {v.operator && <div style={{ marginTop: 6 }}><b>Operator:</b> {v.operator.refund ? "refunded" : "upheld the charge"} {v.operator.note}</div>}
        </div>
      )}
      {["charged"].includes(r.state) && !v && (
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn danger" disabled={busy} onClick={() => dispute("not_me")}>Not me</button>
          <button className="btn danger" disabled={busy} onClick={() => dispute("wrong_time")}>Wrong time</button>
          {busy && <span style={{ fontSize: 12, color: "#6b6d74" }}>AI is reviewing the evidence…</span>}
        </div>
      )}
      {err && <div style={{ color: "#c62b20", fontSize: 12, marginTop: 6 }}>{err}</div>}
    </div>
  );
}

export default function Driver() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const d = params.get("d"), t = params.get("t");
  const [me, setMe] = useState<any>(null);
  const [demo, setDemo] = useState<any[]>([]);
  const [form, setForm] = useState({ plate: "", display_name: "", consent: false });
  const [err, setErr] = useState(params.get("error") ? "PayPal didn't save your account. Try again." : "");
  const [busy, setBusy] = useState(false);

  const load = () => d && t && api(`/api/drivers/${d}?t=${encodeURIComponent(t)}`).then(setMe).catch((e) => setErr(e.message));
  useEffect(() => { api("/api/demo/drivers").then(setDemo); }, []);
  useEffect(() => { setMe(null); load(); if (!d) return; const id = setInterval(load, 3000); return () => clearInterval(id); /* eslint-disable-next-line */ }, [d, t]);

  async function enrol() {
    setBusy(true); setErr("");
    try {
      const r = await api("/api/drivers/enrol", { body: { ...form, lot_ids: [] } });
      window.location.href = r.approve_url;
    } catch (e: any) { setErr(e.message); setBusy(false); }
  }
  async function del() {
    if (!d || !t || !confirm("Delete your plate, PayPal link and stored evidence?")) return;
    await api(`/api/drivers/${d}?t=${encodeURIComponent(t)}`, { method: "DELETE" });
    nav("/driver");
  }

  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Driver</h1><p className="sub">Enrol once: plate plus PayPal. After that there's no app to open and no QR code to trust. Your receipt arrives when you drive out.</p></div>
      </div>
      <div className="phone-wrap">
        <div className="phone">
          <div className="screen">
            <div className="notch" />
            <div className="app-head">
              <div className="row" style={{ gap: 8 }}><span className="brand-mark">D</span><b style={{ font: "800 20px var(--display)", textTransform: "uppercase" }}>Drive-Off</b></div>
              {me && <span className="chip">{me.plate}</span>}
            </div>
            <div className="body">
              {!d && (
                <div className="receipt" style={{ paddingBottom: 18 }}>
                  <div className="lot" style={{ fontSize: 18, marginBottom: 10 }}>Link your plate to PayPal</div>
                  <label>Licence plate</label>
                  <input value={form.plate} placeholder="ABC1234" onChange={(e) => setForm({ ...form, plate: e.target.value.toUpperCase() })} />
                  <div style={{ height: 8 }} />
                  <label>Name (optional)</label>
                  <input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
                  <ul style={{ fontSize: 12, color: "#4b4d54", paddingLeft: 18, margin: "12px 0" }}>{CONSENT.map((c) => <li key={c} style={{ marginBottom: 4 }}>{c}</li>)}</ul>
                  <label style={{ display: "flex", gap: 8, alignItems: "flex-start", textTransform: "none", letterSpacing: 0, fontSize: 13, color: "#15161a" }}>
                    <input type="checkbox" style={{ width: "auto", marginTop: 3 }} checked={form.consent} onChange={(e) => setForm({ ...form, consent: e.target.checked })} />
                    I agree to plate reading and evidence retention as described above.
                  </label>
                  <button className="btn primary" style={{ width: "100%", justifyContent: "center", marginTop: 12, padding: 12, background: "#0070e0", borderColor: "#0070e0" }}
                    disabled={!form.consent || form.plate.length < 2 || busy} onClick={enrol}>
                    {busy ? "Opening PayPal…" : "Link PayPal"}
                  </button>
                  <p style={{ fontSize: 11.5, color: "#6b6d74", margin: "8px 0 0" }}>PayPal places a $1.00 hold to check the account and releases it right away. Sandbox only.</p>
                  {err && <p style={{ color: "#c62b20", fontSize: 12 }}>{err}</p>}
                </div>
              )}
              {me && (
                <>
                  {params.get("enrolled") && <div className="verdict-box good"><b>You're set.</b> PayPal is linked to {me.plate}. Just drive in.</div>}
                  <div style={{ fontSize: 12, color: "#6b6d74", display: "flex", justifyContent: "space-between" }}>
                    <span>PayPal {me.paypal.linked ? "linked ✓" : "not linked"}</span>
                    <button onClick={del} style={{ background: "none", border: 0, color: "#c62b20", fontSize: 12, padding: 0 }}>Delete my data</button>
                  </div>
                  {me.sessions.length === 0 && <div className="receipt"><div className="meta">No parking yet. Drive into any Drive-Off lot.</div></div>}
                  {me.sessions.map((r: any) => <Receipt key={r.id} r={r} t={t!} onChange={load} />)}
                </>
              )}
            </div>
          </div>
        </div>
        <div className="stack" style={{ flex: 1, minWidth: 280, maxWidth: 520 }}>
          <div className="card">
            <h3>Demo drivers (fictional)</h3>
            <div className="row">
              {demo.map((x) => (
                <button key={x.id} className={`btn ${d === x.id ? "primary" : ""}`} onClick={() => nav(`/driver?d=${x.id}&t=${x.access_token}`)}>
                  {x.display_name} <span className="mono muted">{x.plate}</span>
                </button>
              ))}
              <button className={`btn ghost ${!d ? "primary" : ""}`} onClick={() => nav("/driver")}>Enrol a new plate</button>
            </div>
            <p className="small muted" style={{ marginBottom: 0 }}>The demo drivers share one PayPal sandbox buyer account, so their charges and refunds are real sandbox transactions.</p>
          </div>
          <div className="card">
            <h3>Why no QR code</h3>
            <p className="small" style={{ margin: 0 }}>Fake QR stickers on parking meters send people's card details to scammers, and cities from Fort Lauderdale to Austin have warned drivers about them. Drive-Off has nothing to scan: the camera reads your plate and PayPal pays the lot directly.</p>
          </div>
        </div>
      </div>
    </div>
  );
}
