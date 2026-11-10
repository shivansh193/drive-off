import { useEffect, useRef, useState } from "react";
import { api } from "../api";

type Overlay = { frame: number; box: number[]; text: string; conf: number };
type Result = { decision: string; plate?: string | null; confidence: number; agree: number; frames: number; reason?: string; overlay: Overlay[] };

const W = 1280, H = 720;

/** Camera panel: plays a demo clip (or the webcam) with plate boxes, read confidence and the decision. */
export default function Camera({ clip, result, mode, onWebcamResult, lane, lotId }: {
  clip: any | null; result: Result | null; mode: "clips" | "webcam";
  onWebcamResult: (r: any) => void; lane: "entry" | "exit"; lotId: string;
}) {
  const [frame, setFrame] = useState(0);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [busy, setBusy] = useState(false);
  const [camErr, setCamErr] = useState("");
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (mode !== "clips" || !clip) return;
    setFrame(0);
    const id = setInterval(() => setFrame((f) => (f + 1) % clip.frames), 280);
    return () => clearInterval(id);
  }, [clip, mode]);

  useEffect(() => {
    if (mode !== "webcam") { stream?.getTracks().forEach((t) => t.stop()); setStream(null); return; }
    navigator.mediaDevices?.getUserMedia({ video: { width: 1280, height: 720 } })
      .then((s) => { setStream(s); if (video.current) video.current.srcObject = s; })
      .catch((e) => setCamErr(`Camera unavailable: ${e.message}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  useEffect(() => { if (video.current && stream) video.current.srcObject = stream; }, [stream]);

  async function capture(commit: boolean) {
    if (!video.current) return;
    setBusy(true);
    const frames: string[] = [];
    const c = document.createElement("canvas");
    c.width = video.current.videoWidth || W; c.height = video.current.videoHeight || H;
    for (let i = 0; i < 4; i++) {
      c.getContext("2d")!.drawImage(video.current, 0, 0, c.width, c.height);
      frames.push(c.toDataURL("image/jpeg", 0.85));
      await new Promise((r) => setTimeout(r, 250));
    }
    try { onWebcamResult({ ...(await api("/api/vision/read", { body: { lot_id: lotId, lane, frames_base64: frames, commit } })), w: c.width, h: c.height }); }
    finally { setBusy(false); }
  }

  const fw = (result as any)?.w ?? W, fh = (result as any)?.h ?? H;
  const boxes = result?.overlay.filter((o) => mode === "webcam" ? o.frame === 0 : o.frame === frame) ?? [];
  const cls = result ? (result.decision === "review" ? "review" : ["not_enrolled", "no_read"].includes(result.decision) ? "masked" : "") : "";
  const verdict: Record<string, string> = { auto: "Read · matched", review: "Held for review", not_enrolled: "Not enrolled", no_read: "No plate", confirmed: "Confirmed" };

  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="camera">
        {mode === "clips" && clip && <img src={`/clips/${clip.id}/${String(frame).padStart(2, "0")}.jpg`} alt={clip.title} />}
        {mode === "clips" && !clip && <div style={{ display: "grid", placeItems: "center", height: "100%", color: "#777" }}>Pick a clip to drive a car past the camera</div>}
        {mode === "webcam" && <video ref={video} autoPlay muted playsInline />}
        <div className="hud" />
        <div className="rec">{mode === "webcam" ? "LIVE · WEBCAM" : `${(clip?.lane ?? lane).toUpperCase()} LANE · CAM 1`}</div>
        {boxes.map((o, i) => (
          <div key={i} className={`box ${cls}`} style={{
            left: `${(o.box[0] / fw) * 100}%`, top: `${(o.box[1] / fh) * 100}%`,
            width: `${((o.box[2] - o.box[0]) / fw) * 100}%`, height: `${((o.box[3] - o.box[1]) / fh) * 100}%`,
          }}><span>{o.text} · {o.conf.toFixed(2)}</span></div>
        ))}
        {result && (
          <div className="banner" key={JSON.stringify(result.overlay.length) + result.decision + (result.plate ?? "")}>
            <div className={`verdict v-${result.decision}`}>{verdict[result.decision] ?? result.decision}</div>
            <div className="why">
              {result.agree}/{result.frames} frames agree · conf {result.confidence.toFixed(2)}
              {result.reason ? <><br />{result.reason}</> : null}
            </div>
          </div>
        )}
      </div>
      {mode === "webcam" && (
        <div className="row">
          {camErr && <span className="muted small">{camErr}</span>}
          <button className="btn" disabled={busy || !stream} onClick={() => capture(false)}>Read plate (dry run)</button>
          <button className="btn primary" disabled={busy || !stream} onClick={() => capture(true)}>Read &amp; record {lane}</button>
          <span className="muted small">Hold a printed fictional plate (format ABC1234) up to the camera. Nothing is stored unless the plate is enrolled.</span>
        </div>
      )}
    </div>
  );
}
