"""Drive-Off API: web app backend, demo controls and the camera-vendor 'plate event -> charge' API."""
from __future__ import annotations

import base64
import os
import secrets
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, demo, disputes, engine, paypal, signparse, vision
from .rates import RateRule

app = FastAPI(
    title="Drive-Off API",
    version="1.0.0",
    description="Camera-billed parking with PayPal. Camera vendors and lot software post plate events; "
                "Drive-Off matches them to enrolled drivers, prices the stay at the posted rate and charges the "
                "driver's vaulted PayPal account on exit. Sandbox only.",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
LOT_API_KEY = os.environ.get("LOT_API_KEY", "demo-lot-key")
CONSENT_VERSION = "v1"


@app.on_event("startup")
def _startup():
    db.conn()
    demo.seed(os.environ.get("DEMO_VAULT_ID"))
    demo.build_clips()
    engine.purge_expired_evidence()
    threading.Thread(target=vision.alpr, daemon=True).start()  # warm the models


# ------------------------------------------------------------------ helpers
def _decode(b64: str) -> np.ndarray:
    if "," in b64[:64]:
        b64 = b64.split(",", 1)[1]
    arr = np.frombuffer(base64.b64decode(b64), np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "could not decode image")
    return img


def _overlay(read: vision.PassRead, decision: str) -> list[dict]:
    """Boxes for the camera panel. Plates that aren't enrolled are masked."""
    hide = decision in ("not_enrolled", "no_read")
    out = []
    for r in read.reads:
        show = r.text if not hide or r.text != read.plate else "•" * len(r.text)
        if hide:
            show = "not enrolled"
        out.append({"frame": r.frame, "box": r.box, "text": show, "conf": r.ocr_conf})
    return out


def _driver_auth(driver_id: str, t: str | None) -> dict:
    d = db.one("SELECT * FROM drivers WHERE id=?", driver_id)
    if not d or not t or not secrets.compare_digest(d["access_token"] or "", t):
        raise HTTPException(404, "not found")
    return d


def _session_auth(session_id: str, t: str | None) -> dict:
    s = engine.session_out(session_id)
    if not s:
        raise HTTPException(404, "not found")
    d = db.one("SELECT access_token FROM drivers WHERE id=?", s["driver_id"])
    demo_ok = t == "operator"  # demo: the operator console can open any receipt
    if not demo_ok and (not d or not t or not secrets.compare_digest(d["access_token"] or "", t)):
        raise HTTPException(404, "not found")
    return s


def _evidence_urls(event_id: str | None) -> dict:
    if not event_id:
        return {}
    d = config.EVIDENCE_DIR / event_id
    if not d.exists():
        return {}
    return {p.stem: f"/api/evidence/{event_id}/{p.name}" for p in d.iterdir()}


def _receipt(s: dict) -> dict:
    lot = engine.lot(s["lot_id"])
    entry = db.one("SELECT id, at, confidence, agree, frames, vehicle_color, reason FROM events WHERE id=?",
                   s["entry_event_id"]) if s["entry_event_id"] else None
    exit_ = db.one("SELECT id, at, confidence, agree, frames, vehicle_color, reason FROM events WHERE id=?",
                   s["exit_event_id"]) if s["exit_event_id"] else None
    rule = RateRule.from_dict(s["rule_json"]) if s["rule_json"] else engine.rule_for(lot)
    return {**s, "lot": {"id": lot["id"], "name": lot["name"], "rate_sign": lot["rate_sign"]},
            "rule_text": rule.describe(),
            "entry": entry and {**entry, "evidence": _evidence_urls(entry["id"])},
            "exit": exit_ and {**exit_, "evidence": _evidence_urls(exit_["id"])},
            "paypal": {"mode": paypal.mode(), "order_id": s["order_id"], "capture_id": s["capture_id"],
                       "refund_id": s["refund_id"],
                       "sandbox_activity": "https://www.sandbox.paypal.com/myaccount/activities/"}}


# ------------------------------------------------------------------ state for the UI
@app.get("/api/state", tags=["ui"])
def state(lot_id: str = "lot_main"):
    lot = engine.lot(lot_id)
    now = engine.lot_now(lot_id)
    sessions = db.q("SELECT s.*, d.display_name FROM sessions s JOIN drivers d ON d.id=s.driver_id "
                    "WHERE s.lot_id=? ORDER BY s.entry_at", lot_id)
    for s in sessions:
        for k in ("quote_json", "rule_json", "dispute_json", "flags"):
            s[k] = db.j(s[k])
    events = db.q("SELECT id, lane, at, source, plate, plate_hash, confidence, agree, frames, decision, reason, "
                  "session_id, vehicle_color, review_state FROM events WHERE lot_id=? ORDER BY created_at DESC "
                  "LIMIT 60", lot_id)
    anon = db.q("SELECT * FROM anon_presence WHERE lot_id=? ORDER BY entry_at", lot_id)
    active = sum(1 for s in sessions if s["state"] == "active") + sum(1 for a in anon if not a["exit_at"])
    today = now.date().isoformat()
    revenue = sum((s["amount_cents"] or 0) - (s["refund_cents"] or 0) for s in sessions
                  if s["state"] in ("charged", "refunded") and s["capture_id"])
    return {
        "lot": {k: lot[k] for k in ("id", "name", "spaces", "timezone", "plate_format", "rate_sign")}
               | {"rule": db.j(lot["rule_json"]), "rule_text": engine.rule_for(lot).describe(),
                  "rule_confirmed_at": lot["rule_confirmed_at"]},
        "now": db.iso(now), "demo_offset_min": lot["demo_offset_min"], "today": today,
        "sessions": sessions, "events": events, "anon": anon,
        "kpis": {"occupied": active, "spaces": lot["spaces"], "revenue_cents": revenue,
                 "sessions": len(sessions), "review_open": sum(1 for e in events if e["review_state"] == "open"),
                 "disputes": sum(1 for s in sessions if s["dispute_json"])},
        "paypal_mode": paypal.mode(), "gemini": bool(config.GEMINI_API_KEY),
        "lots": db.q("SELECT id, name FROM lots"),
    }


@app.get("/api/clips", tags=["demo"])
def clips():
    return demo.clip_list()


app.mount("/clips", StaticFiles(directory=config.CLIPS_DIR), name="clips")


class PlayIn(BaseModel):
    lot_id: str = "lot_main"
    clip_id: str


@app.post("/api/demo/play", tags=["demo"])
def play(body: PlayIn):
    frames = demo.clip_frames(body.clip_id)
    if not frames:
        raise HTTPException(404, "unknown clip")
    meta = next(c for c in demo.clip_list() if c["id"] == body.clip_id)
    L = engine.lot(body.lot_id)
    read = vision.read_pass(frames, L["plate_format"])
    ev = engine.process_pass(body.lot_id, meta["lane"], frames, source=f"clip:{body.clip_id}", read=read)
    return {"event": ev, "overlay": _overlay(read, ev["decision"]), "clip": meta}


class ClockIn(BaseModel):
    lot_id: str = "lot_main"
    minutes: int = Field(..., ge=-10000, le=10000)


@app.post("/api/demo/clock", tags=["demo"])
def clock(body: ClockIn):
    demo.advance_clock(body.lot_id, body.minutes)
    return {"now": db.iso(engine.lot_now(body.lot_id))}


@app.post("/api/demo/reset", tags=["demo"])
def reset():
    real = db.q("SELECT * FROM drivers WHERE access_token NOT LIKE 'demo-%'")
    db.reset()
    shutil.rmtree(config.EVIDENCE_DIR, ignore_errors=True)
    config.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    demo.seed(os.environ.get("DEMO_VAULT_ID"))
    for d in real:  # keep drivers who enrolled themselves
        db.insert("drivers", d)
    return {"ok": True}


@app.post("/api/demo/sweep", tags=["demo"])
def sweep(lot_id: str = "lot_main"):
    return {"missing_exit": engine.sweep_missing_exits(lot_id)}


# ------------------------------------------------------------------ live camera (webcam)
class FramesIn(BaseModel):
    lot_id: str = "lot_main"
    lane: Literal["entry", "exit"] = "entry"
    frames_base64: list[str] = Field(..., min_length=1, max_length=12)
    commit: bool = False


@app.post("/api/vision/read", tags=["ui"])
def vision_read(body: FramesIn):
    frames = [_decode(f) for f in body.frames_base64]
    L = engine.lot(body.lot_id)
    read = vision.read_pass(frames, L["plate_format"])
    dec = vision.decide(read, set(engine.enrolled(body.lot_id)), L["plate_format"])
    out = {"plate": read.plate if dec.outcome not in ("not_enrolled", "no_read") else None,
           "confidence": read.confidence, "agree": read.agree, "frames": read.frames, "decision": dec.outcome,
           "reasons": dec.reasons, "overlay": _overlay(read, dec.outcome)}
    if body.commit:
        out["event"] = engine.process_pass(body.lot_id, body.lane, frames, source="webcam", read=read)
    return out


# ------------------------------------------------------------------ driver
class EnrolIn(BaseModel):
    plate: str = Field(..., min_length=2, max_length=10)
    display_name: str = Field("", max_length=60)
    lot_ids: list[str] = []
    consent: bool


@app.post("/api/drivers/enrol", tags=["driver"])
def enrol(body: EnrolIn):
    if not body.consent:
        raise HTTPException(400, "consent is required")
    plate = vision.norm(body.plate)
    existing = db.one("SELECT * FROM drivers WHERE plate=?", plate)
    if existing and existing["status"] == "active":
        raise HTTPException(409, "this plate is already enrolled")
    if existing:
        db.exec_("DELETE FROM drivers WHERE id=?", existing["id"])
    did = db.new_id("drv")
    token = secrets.token_urlsafe(18)
    order = paypal.create_enrol_order(did, f"{config.APP_BASE_URL}/api/paypal/return?driver={did}",
                                      f"{config.APP_BASE_URL}/driver?cancelled=1")
    db.insert("drivers", {"id": did, "plate": plate, "display_name": body.display_name or plate,
                          "lot_ids": body.lot_ids, "status": "pending_paypal", "enrol_order_id": order["id"],
                          "consent_at": db.iso(db.now()), "consent_version": CONSENT_VERSION,
                          "created_at": db.iso(db.now()), "access_token": token})
    return {"driver_id": did, "token": token, "approve_url": order["approve_url"]}


@app.get("/api/paypal/return", tags=["driver"], include_in_schema=False)
def paypal_return(driver: str, token: str | None = None):
    d = db.one("SELECT * FROM drivers WHERE id=?", driver)
    if not d or d["enrol_order_id"] != token:
        raise HTTPException(400, "unknown enrolment")
    if d["status"] != "active":
        res = paypal.finish_enrolment(d["enrol_order_id"])
        if not res.get("vault_id"):
            db.audit("enrol_failed", driver, res)
            return RedirectResponse(f"/driver?error=vault&d={driver}")
        db.update("drivers", driver, vault_id=res["vault_id"], paypal_customer_id=res["customer_id"],
                  payer_email=res["payer_email"], status="active")
        db.audit("enrolled", driver, {k: v for k, v in res.items() if k != "payer_email"})
    return RedirectResponse(f"/driver?d={driver}&t={d['access_token']}&enrolled=1")


@app.get("/api/drivers/{driver_id}", tags=["driver"])
def driver(driver_id: str, t: str):
    d = _driver_auth(driver_id, t)
    sessions = db.q("SELECT id FROM sessions WHERE driver_id=? ORDER BY entry_at DESC", driver_id)
    return {"id": d["id"], "plate": d["plate"], "display_name": d["display_name"], "status": d["status"],
            "paypal": {"linked": bool(d["vault_id"]), "payer": d["payer_email"]},
            "consent_at": d["consent_at"], "sessions": [_receipt(engine.session_out(s["id"])) for s in sessions]}


@app.get("/api/demo/drivers", tags=["demo"])
def demo_drivers():
    """Demo shortcut: the fictional demo drivers' phone views."""
    return db.q("SELECT id, display_name, plate, access_token FROM drivers WHERE access_token LIKE 'demo-%'")


@app.delete("/api/drivers/{driver_id}", tags=["driver"])
def delete_driver(driver_id: str, t: str):
    """Right to delete: removes the plate, the PayPal token reference and stored evidence (except open disputes)."""
    d = _driver_auth(driver_id, t)
    if d["vault_id"] and not d["access_token"].startswith("demo-") and paypal.mode() != "mock":
        try:
            paypal._call("DELETE", f"/v3/vault/payment-tokens/{d['vault_id']}")
        except Exception:
            pass
    for s in db.q("SELECT * FROM sessions WHERE driver_id=?", driver_id):
        if s["state"] in ("disputed", "review", "active", "pending_confirm"):
            continue
        for ev in (s["entry_event_id"], s["exit_event_id"]):
            if ev:
                shutil.rmtree(config.EVIDENCE_DIR / ev, ignore_errors=True)
        db.update("sessions", s["id"], plate=vision.plate_hash(s["plate"]))
    db.update("drivers", driver_id, plate=f"deleted-{driver_id}", vault_id=None, payer_email=None,
              display_name="deleted", status="deleted")
    return {"deleted": True}


@app.get("/api/sessions/{session_id}/receipt", tags=["driver"])
def receipt(session_id: str, t: str):
    return _receipt(_session_auth(session_id, t))


class DisputeIn(BaseModel):
    t: str
    reason: Literal["not_me", "wrong_time"]
    note: str = ""
    claimed_exit: datetime | None = None


@app.post("/api/sessions/{session_id}/dispute", tags=["driver"])
def dispute(session_id: str, body: DisputeIn):
    _session_auth(session_id, body.t)
    try:
        verdict = disputes.open_dispute(session_id, body.reason, body.note, body.claimed_exit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"verdict": verdict, "receipt": _receipt(engine.session_out(session_id))}


class ConfirmIn(BaseModel):
    t: str
    exit_at: datetime | None = None


@app.post("/api/sessions/{session_id}/confirm-exit", tags=["driver"])
def confirm_exit(session_id: str, body: ConfirmIn):
    _session_auth(session_id, body.t)
    try:
        engine.confirm_exit(session_id, body.exit_at)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _receipt(engine.session_out(session_id))


@app.get("/api/evidence/{event_id}/{name}", tags=["driver"], include_in_schema=False)
def evidence(event_id: str, name: str):
    if "/" in name or ".." in name or ".." in event_id:
        raise HTTPException(400)
    p = config.EVIDENCE_DIR / event_id / name
    if not p.exists():
        raise HTTPException(404, "evidence deleted or never kept")
    return FileResponse(p, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ operator
class SignIn(BaseModel):
    sign: str = Field(..., min_length=3, max_length=500)


@app.post("/api/lots/{lot_id}/parse-sign", tags=["operator"])
def parse_sign(lot_id: str, body: SignIn):
    L = engine.lot(lot_id)
    return signparse.parse_sign(body.sign, L["timezone"])


class RuleIn(BaseModel):
    sign: str
    rule: dict


@app.post("/api/lots/{lot_id}/rule", tags=["operator"])
def confirm_rule(lot_id: str, body: RuleIn):
    rule = RateRule.from_dict({**body.rule, "sign_text": body.sign})
    errs = rule.validate()
    if errs:
        raise HTTPException(400, "; ".join(errs))
    db.update("lots", lot_id, rate_sign=body.sign, rule_json=rule.to_dict(), rule_confirmed_at=db.iso(db.now()))
    db.audit("rule_confirmed", lot_id, rule.to_dict())
    return {"rule": rule.to_dict(), "plain_english": rule.describe()}


@app.get("/api/review", tags=["operator"])
def review_queue(lot_id: str = "lot_main"):
    out = []
    for e in db.q("SELECT * FROM events WHERE lot_id=? AND review_state='open' ORDER BY at DESC", lot_id):
        e["read_json"] = db.j(e["read_json"])
        e["evidence"] = _evidence_urls(e["id"])
        out.append(e)
    held = [_receipt(engine.session_out(s["id"])) for s in
            db.q("SELECT id FROM sessions WHERE lot_id=? AND state IN ('review','disputed','payment_failed')", lot_id)]
    return {"events": out, "sessions": held}


class ReviewIn(BaseModel):
    plate: str | None = None  # None = reject (not an enrolled vehicle / unreadable)


@app.post("/api/review/{event_id}", tags=["operator"])
def resolve(event_id: str, body: ReviewIn):
    try:
        return engine.resolve_review(event_id, body.plate, "operator")
    except ValueError as e:
        raise HTTPException(400, str(e))


class OperatorIn(BaseModel):
    action: Literal["charge", "refund", "uphold", "release"]
    note: str = ""


@app.post("/api/sessions/{session_id}/operator", tags=["operator"])
def operator_action(session_id: str, body: OperatorIn):
    s = engine.session_out(session_id)
    if not s:
        raise HTTPException(404)
    if body.action == "charge" and s["state"] == "review":
        return engine.charge_session(session_id, db.parse(s["exit_at"]))
    if body.action == "release" and s["state"] == "review":
        db.update("sessions", session_id, state="void", amount_cents=0)
        return {"released": True}
    if body.action in ("refund", "uphold") and s["state"] == "disputed":
        return disputes.operator_decide(session_id, body.action == "refund", body.note)
    raise HTTPException(400, f"cannot {body.action} a session in state {s['state']}")


@app.get("/api/ledger", tags=["operator"])
def ledger(lot_id: str = "lot_main"):
    rows = []
    for s in db.q("SELECT s.*, e1.confidence AS entry_conf, e2.confidence AS exit_conf FROM sessions s "
                  "LEFT JOIN events e1 ON e1.id=s.entry_event_id LEFT JOIN events e2 ON e2.id=s.exit_event_id "
                  "WHERE s.lot_id=? ORDER BY s.entry_at DESC", lot_id):
        dj = db.j(s["dispute_json"])
        rows.append({"session": s["id"], "plate_hash": vision.plate_hash(s["plate"]), "entry": s["entry_at"],
                     "exit": s["exit_at"], "state": s["state"], "amount": (s["amount_cents"] or 0) / 100,
                     "refunded": (s["refund_cents"] or 0) / 100, "rule": RateRule.from_dict(db.j(s["rule_json"])).describe()
                     if s["rule_json"] else "", "order_id": s["order_id"], "capture_id": s["capture_id"],
                     "refund_id": s["refund_id"], "entry_conf": s["entry_conf"], "exit_conf": s["exit_conf"],
                     "flags": ", ".join(db.j(s["flags"], [])), "dispute": dj["outcome"] if dj else ""})
    return rows


@app.get("/api/eval", tags=["operator"])
def eval_results():
    p = config.ROOT / "docs" / "eval.json"
    if not p.exists():
        raise HTTPException(404, "run scripts/run_eval.py")
    return JSONResponse(content=__import__("json").loads(p.read_text()))


@app.get("/api/audit", tags=["operator"])
def audit(limit: int = 80):
    return db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", limit)


@app.get("/api/webhooks", tags=["operator"])
def webhooks():
    return db.q("SELECT * FROM webhooks ORDER BY received_at DESC LIMIT 50")


# ------------------------------------------------------------------ PayPal webhooks
@app.post("/api/paypal/webhook", tags=["paypal"], include_in_schema=False)
async def paypal_webhook(request: Request):
    event = await request.json()
    ok, how = paypal.verify_webhook(dict(request.headers), event)
    et = event.get("event_type", "")
    res = event.get("resource", {}) or {}
    rid = res.get("id")
    db.insert("webhooks", {"id": event.get("id") or db.new_id("wh"), "event_type": et, "resource_id": rid,
                           "verified": int(ok), "verification": how, "received_at": db.iso(db.now()),
                           "summary": f"{res.get('status', '')} {res.get('amount', {}).get('value', '')}".strip()})
    if not ok:
        return {"received": True, "verified": False}
    if et == "PAYMENT.CAPTURE.COMPLETED":
        db.exec_("UPDATE sessions SET capture_status='COMPLETED (webhook)' WHERE capture_id=?", rid)
    elif et in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.REFUND.COMPLETED"):
        cap = next((l["href"].rsplit("/", 1)[-1] for l in res.get("links", []) if l.get("rel") == "up"), rid)
        db.exec_("UPDATE sessions SET capture_status='REFUNDED (webhook)' WHERE capture_id=? OR refund_id=?", cap, rid)
    elif et == "VAULT.PAYMENT-TOKEN.CREATED":
        db.audit("vault_token_webhook", rid or "", {"customer": res.get("customer", {}).get("id")})
    return {"received": True, "verified": True}


# ------------------------------------------------------------------ integration API (APIMatic SDKs)
class VendorRead(BaseModel):
    plate: str
    confidence: float = Field(..., ge=0, le=1)


class PlateEvent(BaseModel):
    lane: Literal["entry", "exit"]
    captured_at: datetime | None = Field(None, description="Camera timestamp; defaults to now")
    camera_id: str = "cam-1"
    frames_base64: list[str] | None = Field(None, max_length=12,
                                            description="JPEG frames of one pass; Drive-Off reads the plate")
    reads: list[VendorRead] | None = Field(None, max_length=12,
                                           description="Per-frame reads from on-camera ANPR, if the camera reads plates itself")


class PlateEventResult(BaseModel):
    event_id: str
    decision: str = Field(description="auto | review | not_enrolled | no_read | confirmed")
    reason: str | None
    session_id: str | None
    session_state: str | None
    amount_cents: int | None
    paypal_capture_id: str | None


@app.post("/v1/lots/{lot_id}/plate-events", response_model=PlateEventResult, tags=["integration"],
          summary="Post a plate event (entry or exit pass)")
def plate_event(lot_id: str, body: PlateEvent, x_lot_key: str = Header(..., alias="X-Lot-Key")):
    """Send one vehicle pass. Drive-Off votes across frames, matches enrolled drivers, opens the session on entry,
    and on exit prices the stay at the posted rate and charges PayPal. Retries are safe: a duplicate exit returns
    the original capture."""
    if not secrets.compare_digest(x_lot_key, LOT_API_KEY):
        raise HTTPException(401, "bad X-Lot-Key")
    L = engine.lot(lot_id)
    if body.frames_base64:
        frames = [_decode(f) for f in body.frames_base64]
        ev = engine.process_pass(lot_id, body.lane, frames, body.captured_at, source=f"api:{body.camera_id}")
    elif body.reads:
        from collections import Counter
        texts = [vision.correct(r.plate, L["plate_format"]) for r in body.reads]
        votes = Counter(texts)
        plate, agree = votes.most_common(1)[0]
        confs = [r.confidence for r, t in zip(body.reads, texts) if t == plate]
        read = vision.PassRead(plate, round(sum(confs) / len(confs), 4), agree, len(body.reads),
                               {t: {"votes": n, "mean_conf": 0} for t, n in votes.items()}, [])
        blank = [np.full((360, 640, 3), 90, np.uint8)]
        ev = engine.process_pass(lot_id, body.lane, blank, body.captured_at, source=f"api:{body.camera_id}", read=read)
    else:
        raise HTTPException(400, "send frames_base64 or reads")
    s = ev.get("session") or {}
    return PlateEventResult(event_id=ev["id"], decision=ev["decision"], reason=ev.get("reason"),
                            session_id=s.get("id"), session_state=s.get("state"), amount_cents=s.get("amount_cents"),
                            paypal_capture_id=s.get("capture_id"))


@app.get("/api/health", tags=["ui"])
def health():
    return {"ok": True, "paypal_mode": paypal.mode(), "gemini": bool(config.GEMINI_API_KEY)}


# ------------------------------------------------------------------ frontend
DIST = config.ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = DIST / path
        if path and f.is_file() and DIST in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
