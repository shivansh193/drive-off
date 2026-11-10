import { useEffect, useState } from "react";
import { api } from "../api";

const pct = (x?: number) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);

export default function Eval() {
  const [e, setE] = useState<any>(null);
  const [err, setErr] = useState("");
  useEffect(() => { api("/api/eval").then(setE).catch((x) => setErr(x.message)); }, []);
  if (err) return <div className="page"><p className="muted">{err}</p></div>;
  if (!e) return <div className="page"><p className="muted">Loading…</p></div>;
  const s = e.single_frame, p = e.passes;
  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Accuracy &amp; safety</h1>
          <p className="sub">{e.note}</p></div>
        <span className="pill">generated {e.generated_at?.slice(0, 10)}</span>
      </div>
      <div className="kpis" style={{ marginBottom: 18 }}>
        <div className="kpi accent"><div className="v">{p.wrong_person_charges}</div><div className="l">Wrong-person auto-charges</div></div>
        <div className="kpi"><div className="v">{pct(p.auto_precision)}</div><div className="l">Auto-charge precision</div></div>
        <div className="kpi"><div className="v">{pct(p.auto_rate)}</div><div className="l">Passes auto-handled</div></div>
        <div className="kpi"><div className="v">{pct(p.review_rate)}</div><div className="l">Sent to review</div></div>
        <div className="kpi"><div className="v">{pct(s.exact)}</div><div className="l">Single-frame exact read</div></div>
      </div>
      <div className="grid-2">
        <div className="card">
          <h2>Plate reads by condition</h2>
          <p className="small muted" style={{ marginTop: 0 }}>Held-out set of {s.n} rendered images (seed {e.seed}), one frame each, no voting and no LLM.</p>
          <table className="simple"><thead><tr><th>Condition</th><th>n</th><th>Detected</th><th>Exact</th><th>Per-char</th></tr></thead>
            <tbody>{Object.entries(s.by_condition).map(([c, v]: any) => (
              <tr key={c}><td>{c}</td><td className="num">{v.n}</td><td className="num">{pct(v.detected)}</td><td className="num">{pct(v.exact)}</td><td className="num">{pct(v.char)}</td></tr>))}
              <tr><td><b>all</b></td><td className="num">{s.n}</td><td className="num">{pct(s.detected)}</td><td className="num"><b>{pct(s.exact)}</b></td><td className="num">{pct(s.char)}</td></tr></tbody></table>
          <h3 style={{ marginTop: 18 }}>Confidence calibration (single frame)</h3>
          <table className="simple"><thead><tr><th>OCR confidence</th><th>n</th><th>Actually correct</th></tr></thead>
            <tbody>{s.calibration.map((b: any) => <tr key={b.bin}><td className="mono">{b.bin}</td><td className="num">{b.n}</td><td className="num">{pct(b.exact)}</td></tr>)}</tbody></table>
          <p className="small muted">A high OCR score isn't enough on its own: some wrong reads still score above 0.95. That's why Drive-Off also requires several frames to agree and an exact match to an enrolled plate.</p>
        </div>
        <div className="card">
          <h2>Auto-charge safety</h2>
          <p className="small muted" style={{ marginTop: 0 }}>{p.n} simulated passes ({p.frames_per_pass} frames each) against {p.enrolled} enrolled plates, including {p.near_pairs} pairs that differ by one character. Rule: ≥{p.min_agree} agreeing frames, confidence ≥{p.min_conf}, exact enrolled match, no other enrolled plate within one character.</p>
          <table className="simple"><thead><tr><th>Outcome</th><th>Passes</th></tr></thead>
            <tbody>{Object.entries(p.outcomes).map(([k, v]: any) => <tr key={k}><td>{k}</td><td className="num">{v}</td></tr>)}</tbody></table>
          <h3 style={{ marginTop: 18 }}>By condition</h3>
          <table className="simple"><thead><tr><th>Condition</th><th>Auto</th><th>Review</th><th>Wrong auto</th></tr></thead>
            <tbody>{Object.entries(p.by_condition).map(([c, v]: any) => <tr key={c}><td>{c}</td><td className="num">{pct(v.auto)}</td><td className="num">{pct(v.review)}</td><td className="num">{v.wrong}</td></tr>)}</tbody></table>
          <h3 style={{ marginTop: 18 }}>Billing &amp; idempotency tests</h3>
          <p className="small" style={{ margin: 0 }}>{e.tests?.summary ?? "see backend/tests"}</p>
          <div className="rule-line" />
          <h3>Limits</h3>
          <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>{e.limits.map((l: string) => <li key={l}>{l}</li>)}</ul>
        </div>
      </div>
    </div>
  );
}
