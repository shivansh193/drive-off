import { useEffect, useMemo, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import { AllCommunityModule, ModuleRegistry, themeQuartz, type ColDef } from "ag-grid-community";
import { api, fmtTime, money } from "../api";
import { useLotState } from "./Lot";

ModuleRegistry.registerModules([AllCommunityModule]);

const gridTheme = themeQuartz.withParams({
  backgroundColor: "#1d1f23", foregroundColor: "#f2f1ec", headerBackgroundColor: "#16171a", borderColor: "#34373f",
  accentColor: "#f5c518", fontFamily: "Overpass", headerFontWeight: 700, rowHoverColor: "#262930", oddRowBackgroundColor: "#1a1c20",
  chromeBackgroundColor: "#16171a", fontSize: 13,
});

const stateCell = (p: any) => <span className={`state s-${p.value}`}>{String(p.value).replace("_", " ")}</span>;

export default function Operator({ toast }: { toast: (s: string) => void }) {
  const lotId = "lot_main";
  const [state, refresh] = useLotState(lotId, 3000);
  const [rows, setRows] = useState<any[]>([]);
  const [review, setReview] = useState<any>({ events: [], sessions: [] });
  const [hooks, setHooks] = useState<any[]>([]);
  const [sign, setSign] = useState("");
  const [parsed, setParsed] = useState<any>(null);
  const [parsing, setParsing] = useState(false);

  const load = () => {
    api(`/api/ledger?lot_id=${lotId}`).then(setRows);
    api(`/api/review?lot_id=${lotId}`).then(setReview);
    api("/api/webhooks").then(setHooks);
  };
  useEffect(() => { load(); const id = setInterval(load, 3000); return () => clearInterval(id); }, []);
  useEffect(() => { if (state && !sign) setSign(state.lot.rate_sign); }, [state]); // eslint-disable-line

  const cols = useMemo<ColDef[]>(() => [
    { field: "plate_hash", headerName: "Plate (hashed)", width: 150, cellClass: "mono" },
    { field: "entry", headerName: "In", valueFormatter: (p) => fmtTime(p.value), width: 140 },
    { field: "exit", headerName: "Out", valueFormatter: (p) => fmtTime(p.value), width: 140 },
    { field: "state", cellRenderer: stateCell, width: 140, filter: true },
    { field: "amount", headerName: "Charged", valueFormatter: (p) => `$${p.value.toFixed(2)}`, type: "numericColumn", width: 110 },
    { field: "refunded", valueFormatter: (p) => (p.value ? `−$${p.value.toFixed(2)}` : ""), type: "numericColumn", width: 110 },
    { field: "entry_conf", headerName: "Read conf in", valueFormatter: (p) => p.value?.toFixed(2) ?? "", width: 120 },
    { field: "exit_conf", headerName: "Read conf out", valueFormatter: (p) => p.value?.toFixed(2) ?? "", width: 125 },
    { field: "flags", width: 160, filter: true },
    { field: "dispute", width: 140, filter: true },
    { field: "capture_id", headerName: "PayPal capture", width: 190, cellClass: "mono" },
    { field: "refund_id", headerName: "PayPal refund", width: 190, cellClass: "mono" },
    { field: "order_id", headerName: "PayPal order", width: 190, cellClass: "mono" },
    { field: "rule", headerName: "Rate rule applied", flex: 1, minWidth: 260 },
  ], []);

  async function parse() {
    setParsing(true); setParsed(null);
    try { setParsed(await api(`/api/lots/${lotId}/parse-sign`, { body: { sign } })); } catch (e: any) { toast(e.message); } finally { setParsing(false); }
  }
  async function confirmRule() {
    await api(`/api/lots/${lotId}/rule`, { body: { sign, rule: parsed.rule } });
    toast("Rate rule is live. Every new exit is priced by it."); setParsed(null); refresh();
  }
  async function resolve(id: string, plate: string | null) {
    try { await api(`/api/review/${id}`, { body: { plate } }); toast(plate ? `Assigned to ${plate}` : "Rejected, evidence deleted"); load(); refresh(); } catch (e: any) { toast(e.message); }
  }
  async function act(id: string, action: string) {
    try { await api(`/api/sessions/${id}/operator`, { body: { action } }); toast(`Session ${action}`); load(); refresh(); } catch (e: any) { toast(e.message); }
  }

  if (!state) return <div className="page"><p className="muted">Loading…</p></div>;
  const k = state.kpis;
  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Operator</h1><p className="sub">{state.lot.name}: one camera and a PayPal account instead of a gate and a kiosk.</p></div>
        <span className={`pill ${state.paypal_mode === "mock" ? "warn" : "ok"}`}><span className="dot" />PayPal {state.paypal_mode}</span>
      </div>
      <div className="kpis" style={{ marginBottom: 18 }}>
        <div className="kpi accent"><div className="v">{Math.round((k.occupied / k.spaces) * 100)}%</div><div className="l">Occupancy · {k.occupied}/{k.spaces}</div><div className="meter"><div style={{ width: `${(k.occupied / k.spaces) * 100}%` }} /></div></div>
        <div className="kpi"><div className="v">{money(k.revenue_cents)}</div><div className="l">Net revenue (PayPal)</div></div>
        <div className="kpi"><div className="v">{rows.filter((r) => r.state === "charged").length}</div><div className="l">Paid sessions</div></div>
        <div className="kpi"><div className="v" style={{ color: review.events.length ? "var(--amber)" : undefined }}>{review.events.length + review.sessions.length}</div><div className="l">Needs a human</div></div>
        <div className="kpi"><div className="v">{hooks.filter((h) => h.verified).length}</div><div className="l">Verified webhooks</div></div>
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <h2>Sessions ledger</h2>
        <div className="ledger"><AgGridReact theme={gridTheme} rowData={rows} columnDefs={cols} defaultColDef={{ sortable: true, resizable: true }} animateRows /></div>
      </div>

      <div className="grid-2">
        <div className="stack">
          <div className="card">
            <h2>Review queue</h2>
            <p className="small muted" style={{ marginTop: 0 }}>Reads below the safety bar are never charged automatically. Gemini gives a second read, and a person decides.</p>
            {review.events.length === 0 && <p className="muted small">Nothing waiting.</p>}
            <div className="stack" style={{ gap: 10 }}>
              {review.events.map((e: any) => {
                const llm = e.read_json?.llm ?? {};
                const near = e.reason?.match(/enrolled plate\(s\) ([A-Z0-9, ]+)/)?.[1]?.split(", ") ?? [];
                const suggest = [...new Set([e.plate, llm.plate?.replace(/\s/g, ""), ...near].filter(Boolean))] as string[];
                return (
                  <div key={e.id} className="review-card">
                    <div>{e.evidence?.crop ? <img src={e.evidence.crop} alt="plate crop" /> : <div className="muted small">no crop</div>}
                      {e.evidence?.frame && <img src={e.evidence.frame} alt="frame" style={{ marginTop: 6 }} />}</div>
                    <div>
                      <div className="row" style={{ justifyContent: "space-between" }}><b>{e.lane.toUpperCase()} · {fmtTime(e.at)}</b><span className="muted small">{e.vehicle_color}</span></div>
                      <div className="small muted">{e.reason}</div>
                      <div className="cands">{Object.entries(e.read_json?.candidates ?? {}).map(([p, c]: any) => <code key={p}>{p} ×{c.votes}</code>)}</div>
                      <div className="small">Gemini second read: {llm.plate ? <b className="mono">{llm.plate}</b> : <span className="muted">{llm.error ?? llm.skipped ?? "—"}</span>} {llm.confidence != null && <span className="muted">({Number(llm.confidence).toFixed(2)})</span>} {llm.notes && <span className="muted">· {llm.notes}</span>}</div>
                      <div className="row" style={{ marginTop: 8 }}>
                        {suggest.map((p) => <button key={p} className="btn primary" onClick={() => resolve(e.id, p)}>It's {p}</button>)}
                        <button className="btn ghost danger" onClick={() => resolve(e.id, null)}>Reject</button>
                      </div>
                    </div>
                  </div>
                );
              })}
              {review.sessions.map((s: any) => (
                <div key={s.id} className="review-card" style={{ borderLeftColor: s.state === "disputed" ? "var(--stop)" : "var(--amber)" }}>
                  <div>{s.exit?.evidence?.annotated && <img src={s.exit.evidence.annotated} alt="exit" />}{s.entry?.evidence?.annotated && <img src={s.entry.evidence.annotated} alt="entry" style={{ marginTop: 6 }} />}</div>
                  <div>
                    <div className="row" style={{ justifyContent: "space-between" }}><b className="mono">{s.plate}</b><span className={`state s-${s.state}`}>{s.state}</span></div>
                    <div className="small muted">Flags: {(s.flags ?? []).join(", ") || "none"} · quote {money(s.amount_cents)}</div>
                    <div className="small muted">Entry car: {s.entry?.vehicle_color} · exit car: {s.exit?.vehicle_color}</div>
                    {s.dispute_json && <div className="small" style={{ marginTop: 4 }}>{s.dispute_json.explanation}</div>}
                    <div className="row" style={{ marginTop: 8 }}>
                      {s.state === "review" && <><button className="btn primary" onClick={() => act(s.id, "charge")}>Charge driver</button><button className="btn" onClick={() => act(s.id, "release")}>Release (no charge)</button></>}
                      {s.state === "disputed" && <><button className="btn primary" onClick={() => act(s.id, "refund")}>Refund</button><button className="btn" onClick={() => act(s.id, "uphold")}>Uphold</button></>}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="card">
            <h2>PayPal webhooks</h2>
            {hooks.length === 0 ? <p className="muted small">No webhook deliveries yet.</p> : (
              <table className="simple"><thead><tr><th>Event</th><th>Resource</th><th>Verified</th><th>At</th></tr></thead>
                <tbody>{hooks.map((h) => <tr key={h.id}><td className="mono small">{h.event_type}</td><td className="mono small">{h.resource_id}</td><td>{h.verified ? <span className="state s-charged">verified</span> : <span className="state s-review">{h.verification}</span>}</td><td className="small">{fmtTime(h.received_at)}</td></tr>)}</tbody></table>
            )}
          </div>
        </div>
        <div className="stack">
          <div className="card lane">
            <h2>Posted rate</h2>
            <p className="small muted" style={{ marginTop: 0 }}>Type the rate as it's written on your sign. Gemini turns it into a billing rule, and you confirm it before it goes live. Drive-Off can only charge what's posted.</p>
            <div className="small" style={{ marginBottom: 10 }}>Live now: <b>{state.lot.rule_text}</b></div>
            <textarea rows={3} value={sign} onChange={(e) => setSign(e.target.value)} />
            <div className="row" style={{ marginTop: 10 }}>
              <button className="btn primary" disabled={parsing} onClick={parse}>{parsing ? "Gemini is reading the sign…" : "Parse sign with AI"}</button>
              <button className="btn ghost" onClick={() => setSign("$2.50 per 30 min, max $18 per day. Free 6pm-8am and all day Sunday. No charge under 10 minutes.")}>Try a harder sign</button>
            </div>
            {parsed && (
              <div className="fade-in" style={{ marginTop: 12 }}>
                <div className="rule-line" />
                <h3>Parsed rule · {parsed.source}{parsed.meta?._cached ? " (cached)" : ""}</h3>
                <p style={{ margin: "0 0 8px", fontWeight: 700 }}>{parsed.plain_english}</p>
                {parsed.questions?.length > 0 && <ul className="small" style={{ color: "var(--amber)" }}>{parsed.questions.map((q: string) => <li key={q}>{q}</li>)}</ul>}
                {parsed.errors?.length > 0 && <p className="small" style={{ color: "var(--stop)" }}>{parsed.errors.join("; ")}</p>}
                <div className="json">{JSON.stringify({ ...parsed.rule, sign_text: undefined }, null, 2)}</div>
                <div className="row" style={{ marginTop: 10 }}>
                  <button className="btn primary" disabled={parsed.errors?.length > 0} onClick={confirmRule}>Confirm and go live</button>
                  <button className="btn ghost" onClick={() => setParsed(null)}>Discard</button>
                </div>
              </div>
            )}
          </div>
          <div className="card">
            <h2>Integrate a camera</h2>
            <p className="small muted" style={{ marginTop: 0 }}>Camera vendors and lot software send passes to one endpoint. SDKs are generated with APIMatic from the OpenAPI spec.</p>
            <div className="json">{`POST /v1/lots/${lotId}/plate-events
X-Lot-Key: demo-lot-key
{ "lane": "exit", "camera_id": "cam-2",
  "reads": [{"plate":"KTR4821","confidence":0.98}, ...] }`}</div>
            <div className="row" style={{ marginTop: 10 }}><a className="btn" href="/docs" target="_blank">OpenAPI docs</a><a className="btn ghost" href="https://github.com/shivansh193/drive-off/tree/main/sdk" target="_blank" rel="noreferrer">SDKs</a></div>
          </div>
        </div>
      </div>
    </div>
  );
}
