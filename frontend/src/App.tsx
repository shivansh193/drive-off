import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { api } from "./api";
import Lot from "./pages/Lot";
import Driver from "./pages/Driver";
import Operator from "./pages/Operator";
import Eval from "./pages/Eval";

export default function App() {
  const [msg, setMsg] = useState<string | null>(null);
  const [health, setHealth] = useState<any>(null);
  const toast = useCallback((s: string) => { setMsg(s); setTimeout(() => setMsg((m) => (m === s ? null : m)), 4200); }, []);
  useEffect(() => { api("/api/health").then(setHealth).catch(() => {}); }, []);
  return (
    <BrowserRouter>
      <header className="topbar">
        <NavLink to="/" className="brand"><span className="brand-mark">D</span><span className="brand-name">Drive-Off</span></NavLink>
        <nav className="nav">
          <NavLink to="/" end>Lot</NavLink>
          <NavLink to="/driver">Driver</NavLink>
          <NavLink to="/operator">Operator</NavLink>
          <NavLink to="/eval">Eval</NavLink>
        </nav>
        <div className="spacer" />
        {health && <>
          <span className={`pill ${health.paypal_mode === "mock" ? "warn" : "ok"}`}><span className="dot" />PayPal {health.paypal_mode}</span>
          <span className={`pill ${health.gemini ? "ok" : "warn"}`}><span className="dot" />Gemini {health.gemini ? "on" : "off"}</span>
        </>}
      </header>
      <Routes>
        <Route path="/" element={<Lot toast={toast} />} />
        <Route path="/driver" element={<Driver />} />
        <Route path="/operator" element={<Operator toast={toast} />} />
        <Route path="/eval" element={<Eval />} />
      </Routes>
      {msg && <div className="toast" role="status">{msg}</div>}
    </BrowserRouter>
  );
}
