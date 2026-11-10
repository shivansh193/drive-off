import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, fmtTime, money } from "../api";
import Camera from "../components/Camera";
import Timeline from "../components/Timeline";

export function useLotState(lotId = "lot_main", every = 2000) {
  const [state, setState] = useState<any>(null);
  const refresh = useCallback(() => api(`/api/state?lot_id=${lotId}`).then(setState).catch(() => {}), [lotId]);
  useEffect(() => { refresh(); const id = setInterval(refresh, every); return () => clearInterval(id); }, [refresh, every]);
  return [state, refresh] as const;
}

export default function Lot({ toast }: { toast: (s: string) => void }) {
  const lotId = "lot_main";
  const [state, refresh] = useLotState(lotId);
  const [clips, setClips] = useState<any[]>([]);
  const [clip, setClip] = useState<any>(null);
  const [result, setResult] = useState<any>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [mode, setMode] = useState<"clips" | "webcam">("clips");
  const [lane, setLane] = useState<"entry" | "exit">("entry");

  useEffect(() => { api("/api/clips").then(setClips); }, []);

  async function play(c: any) {
    setClip(c); setResult(null); setRunning(c.id); setMode("clips");
    try {
      const r = await api("/api/demo/play", { body: { lot_id: lotId, clip_id: c.id } });
      const ev = r.event;
      setResult({ decision: ev.decision, plate: ev.plate, confidence: ev.confidence, agree: ev.agree, frames: ev.frames, reason: ev.reason, overlay: r.overlay });
      const s = ev.session;
      if (s?.state === "charged" && c.lane === "exit" && !ev.reason?.includes("duplicate")) toast(`PayPal charged ${money(s.amount_cents)} · capture ${s.capture_id}`);
      else if (ev.reason?.includes("duplicate")) toast("Duplicate exit read → same PayPal capture returned. No double charge.");
      else if (ev.tailgate_with) toast("Tailgating: two vehicles in one pass, both handled separately");
      refresh();
    } catch (e: any) { toast(e.message); } finally { setRunning(null); }
  }

  async function clock(minutes: number) { await api("/api/demo/clock", { body: { lot_id: lotId, minutes } }); refresh(); }
  async function reset() { await api("/api/demo/reset", { method: "POST" }); setResult(null); setClip(null); refresh(); toast("Demo lot reset"); }
  async function sweep() { const r = await api(`/api/demo/sweep?lot_id=${lotId}`, { method: "POST" }); refresh(); toast(r.missing_exit.length ? `${r.missing_exit.length} session(s) with no exit read → driver asked to confirm` : "No stale sessions"); }

  if (!state) return <div className="page"><p className="muted">Loading lot…</p></div>;
  const k = state.kpis;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>{state.lot.name}</h1>
          <p className="sub">Posted rate: <b style={{ color: "var(--paint)" }}>{state.lot.rate_sign}</b> · the only rule Drive-Off bills by.</p>
        </div>
        <div className="row">
          <span className="pill"><span className="dot" />demo clock {fmtTime(state.now)}</span>
          <button className="btn" onClick={() => clock(30)}>+30 min</button>
          <button className="btn" onClick={() => clock(120)}>+2 h</button>
          <button className="btn" onClick={() => clock(60 * 25)}>+25 h</button>
          <button className="btn ghost" onClick={sweep}>Missing-exit sweep</button>
          <button className="btn ghost danger" onClick={reset}>Reset</button>
        </div>
      </div>

      <div className="kpis" style={{ marginBottom: 18 }}>
        <div className="kpi accent"><div className="v">{k.occupied}<span className="muted" style={{ fontSize: 18 }}>/{k.spaces}</span></div><div className="l">Occupied</div><div className="meter"><div style={{ width: `${(k.occupied / k.spaces) * 100}%` }} /></div></div>
        <div className="kpi"><div className="v">{money(k.revenue_cents)}</div><div className="l">Collected via PayPal</div></div>
        <div className="kpi"><div className="v">{k.sessions}</div><div className="l">Sessions</div></div>
        <div className="kpi"><div className="v" style={{ color: k.review_open ? "var(--amber)" : undefined }}>{k.review_open}</div><div className="l">Review queue</div></div>
        <div className="kpi"><div className="v">{k.disputes}</div><div className="l">Disputes</div></div>
      </div>

      <div className="grid-2">
        <div className="stack">
          <div className="card lane">
            <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
              <h2 style={{ margin: 0 }}>Lane camera</h2>
              <div className="row">
                <button className={`btn ${mode === "clips" ? "primary" : ""}`} onClick={() => { setMode("clips"); setResult(null); }}>Demo clips</button>
                <button className={`btn ${mode === "webcam" ? "primary" : ""}`} onClick={() => { setMode("webcam"); setResult(null); }}>Live webcam</button>
                {mode === "webcam" && <select value={lane} onChange={(e) => setLane(e.target.value as any)} style={{ width: 110 }}><option value="entry">Entry</option><option value="exit">Exit</option></select>}
              </div>
            </div>
            <Camera clip={clip} result={result} mode={mode} lane={lane} lotId={lotId} onWebcamResult={(r) => { setResult({ ...r, reason: r.reasons?.join("; ") }); refresh(); }} />
          </div>
          {mode === "clips" && (
            <div className="card">
              <h3>Drive a car past the camera</h3>
              <div className="clips">
                {clips.map((c) => (
                  <button key={c.id} className={`clip ${running === c.id ? "running" : ""}`} disabled={!!running} onClick={() => play(c)}>
                    <span className="row" style={{ gap: 6 }}><span className={`lane-tag ${c.lane}`} style={{ font: "700 10px var(--mono)", color: c.lane === "entry" ? "var(--go)" : "var(--amber)" }}>{c.lane.toUpperCase()}</span><b>{c.title}</b></span>
                    <small>{c.story}</small>
                  </button>
                ))}
              </div>
              <p className="muted small" style={{ marginBottom: 0 }}>All cars and plates are rendered and fictional. Suggested order: Maya in → Sam in → visitor in → Ana in → +2 h → Maya out → re-read → Sam out at night → green car with Ana's plate → two cars → +25 h → missing-exit sweep → Sam's phone: accept default, then dispute "wrong time".</p>
            </div>
          )}
        </div>

        <div className="stack">
          <div className="card">
            <h3>Lane events</h3>
            <div className="feed">
              {state.events.length === 0 && <p className="muted small">No vehicles yet.</p>}
              {state.events.map((e: any) => (
                <div key={e.id} className={`feed-item d-${e.decision}`}>
                  <span className={`lane ${e.lane}`}>{e.lane}</span>
                  <div className="what">
                    <b className="mono">{e.plate ?? (e.decision === "not_enrolled" ? `#${e.plate_hash?.slice(0, 8)}` : "—")}</b>{" "}
                    <span className="muted">{e.decision.replace("_", " ")}{e.confidence ? ` · ${e.confidence.toFixed(2)}` : ""}{e.vehicle_color ? ` · ${e.vehicle_color}` : ""}</span>
                    {e.reason && <small>{e.reason}</small>}
                  </div>
                  <span className="t">{fmtTime(e.at)}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="card">
            <h3>On the phone</h3>
            <p className="small muted" style={{ marginTop: 0 }}>Receipts land on the driver's phone the moment PayPal captures.</p>
            <Link to="/driver" className="btn">Open driver view →</Link>{" "}
            <Link to="/operator" className="btn ghost">Operator console →</Link>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>Live sessions</h2>
          <div className="legend">
            <span><i style={{ background: "#f5c518" }} />parked</span><span><i style={{ background: "#3ccf7e" }} />charged</span>
            <span><i style={{ background: "#ff9f2e" }} />held</span><span><i style={{ background: "#ff5a4e" }} />disputed</span>
            <span><i style={{ background: "#5aa9ff" }} />refunded</span><span><i style={{ background: "#b48cff" }} />confirm exit</span>
          </div>
        </div>
        <div style={{ marginTop: 12 }}><Timeline sessions={state.sessions} anon={state.anon} now={state.now} /></div>
      </div>
    </div>
  );
}
