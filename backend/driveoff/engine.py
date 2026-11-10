"""Session and billing engine: turns lane passes into sessions, charges and flags."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta

import cv2
import numpy as np

from . import config, db, paypal, vision
from .rates import RateRule, price

DUPLICATE_WINDOW = timedelta(minutes=10)
MISSING_EXIT_AFTER = timedelta(hours=24)


# ------------------------------------------------------------------ helpers
def lot(lot_id: str) -> dict:
    l = db.one("SELECT * FROM lots WHERE id=?", lot_id)
    if not l:
        raise KeyError(f"unknown lot {lot_id}")
    return l


def lot_now(lot_id: str) -> datetime:
    """The lot's clock, including the demo fast-forward offset (shown as 'demo clock' in the UI)."""
    return db.now() + timedelta(minutes=lot(lot_id)["demo_offset_min"] or 0)


def enrolled(lot_id: str) -> dict[str, dict]:
    out = {}
    for d in db.q("SELECT * FROM drivers WHERE status='active'"):
        lots = db.j(d["lot_ids"], [])
        if not lots or lot_id in lots:
            out[d["plate"]] = d
    return out


def rule_for(lot_row: dict) -> RateRule:
    if not lot_row["rule_json"] or not lot_row["rule_confirmed_at"]:
        raise ValueError("lot has no confirmed rate rule")
    return RateRule.from_dict(json.loads(lot_row["rule_json"]))


def _session_flags(s: dict) -> list[str]:
    return db.j(s.get("flags"), []) or []


def _add_flag(session_id: str, flag: str):
    s = db.one("SELECT flags FROM sessions WHERE id=?", session_id)
    flags = _session_flags(s)
    if flag not in flags:
        flags.append(flag)
        db.update("sessions", session_id, flags=flags, updated_at=db.iso(db.now()))


def save_evidence(event_id: str, frames: list[np.ndarray], read: vision.PassRead) -> dict:
    """Keep the minimum: one face-masked frame, one plate crop, one annotated frame."""
    d = config.EVIDENCE_DIR / event_id
    d.mkdir(parents=True, exist_ok=True)
    idx = read.best_frame or 0
    frame = frames[idx]
    best_reads = [r for r in read.reads if r.frame == idx]
    masked, faces = vision.mask_faces(frame)
    cv2.imwrite(str(d / "frame.jpg"), masked, [cv2.IMWRITE_JPEG_QUALITY, 82])
    cv2.imwrite(str(d / "annotated.jpg"), vision.draw(masked, best_reads), [cv2.IMWRITE_JPEG_QUALITY, 82])
    color = "unknown"
    if best_reads:
        top = max(best_reads, key=lambda r: r.ocr_conf)
        cv2.imwrite(str(d / "crop.jpg"), vision.crop(masked, top.box))
        color = vision.vehicle_color(frame, top.box)
    return {"dir": event_id, "files": sorted(p.name for p in d.iterdir()), "faces_masked": faces, "color": color}


def evidence_crops(event_id: str, frames: list[np.ndarray], read: vision.PassRead) -> list[np.ndarray]:
    crops = []
    for r in sorted(read.reads, key=lambda r: -r.ocr_conf)[:4]:
        crops.append(vision.crop(frames[r.frame], r.box))
    return crops


# ------------------------------------------------------------------ the pass
def process_pass(lot_id: str, lane: str, frames: list[np.ndarray], at: datetime | None = None,
                 source: str = "camera", read: vision.PassRead | None = None) -> dict:
    """One vehicle passing the entry or exit camera. Returns the stored event (with session/charge info)."""
    assert lane in ("entry", "exit")
    L = lot(lot_id)
    at = at or lot_now(lot_id)
    fmt = L["plate_format"]
    read = read or vision.read_pass(frames, fmt)
    if read.multi_vehicle and read.second_plate and not source.endswith("+tailgate"):
        # Two vehicles in one pass: treat each plate as its own pass, both flagged as tailgating.
        second = vision.PassRead(
            read.second_plate, read.candidates[read.second_plate]["mean_conf"],
            read.candidates[read.second_plate]["votes"], read.frames,
            {read.second_plate: read.candidates[read.second_plate]},
            [r for r in read.reads if r.text == read.second_plate], True, read.plate,
            next((r.frame for r in read.reads if r.text == read.second_plate), 0))
        first = vision.PassRead(read.plate, read.confidence, read.agree, read.frames,
                                {read.plate: read.candidates[read.plate]},
                                [r for r in read.reads if r.text == read.plate], True, read.second_plate, read.best_frame)
        a = process_pass(lot_id, lane, frames, at, source + "+tailgate", first)
        b = process_pass(lot_id, lane, frames, at, source + "+tailgate", second)
        return {**a, "tailgate_with": b}
    people = enrolled(lot_id)
    dec = vision.decide(read, set(people), fmt)
    ev_id = db.new_id("ev")
    ev = {"id": ev_id, "lot_id": lot_id, "lane": lane, "at": db.iso(at), "source": source,
          "confidence": read.confidence, "agree": read.agree, "frames": read.frames, "decision": dec.outcome,
          "reason": "; ".join(dec.reasons), "created_at": db.iso(db.now())}
    read_json = read.to_dict()

    if dec.outcome in ("not_enrolled", "no_read"):
        # Privacy: no image, no plate text. Only a salted hash, for occupancy counts.
        if dec.outcome == "not_enrolled":
            h = vision.plate_hash(read.plate)
            ev["plate_hash"] = h
            if lane == "entry":
                db.insert("anon_presence", {"plate_hash": h, "lot_id": lot_id, "entry_at": db.iso(at)})
            else:
                db.exec_("UPDATE anon_presence SET exit_at=? WHERE rowid=(SELECT rowid FROM anon_presence "
                         "WHERE plate_hash=? AND lot_id=? AND exit_at IS NULL ORDER BY entry_at DESC LIMIT 1)",
                         db.iso(at), h, lot_id)
        ev["read_json"] = {"frames": read.frames, "agree": read.agree, "confidence": read.confidence}
        db.insert("events", ev)
        return _event_out(ev_id)

    # Could be an enrolled driver: keep minimal evidence for the dispute window.
    evidence = save_evidence(ev_id, frames, read)
    ev.update(plate=read.plate, plate_hash=vision.plate_hash(read.plate), evidence=evidence,
              vehicle_color=evidence["color"])
    if read.multi_vehicle:
        ev["reason"] = (ev["reason"] + "; " if ev["reason"] else "") + f"tailgating: second vehicle {read.second_plate or '?'} in the same pass"

    if dec.outcome == "review":
        read_json["llm"] = vision.llm_second_pass(evidence_crops(ev_id, frames, read), read.candidates, fmt)
        ev.update(review_state="open", read_json=read_json)
        db.insert("events", ev)
        db.audit("review_queued", ev_id, {"reasons": dec.reasons, "suggested_plate": dec.driver_plate})
        return _event_out(ev_id)

    ev["read_json"] = read_json
    db.insert("events", ev)
    result = apply_plate(ev_id, lot_id, lane, read.plate, at, tailgate=read.multi_vehicle)
    if read.multi_vehicle and read.second_plate and read.second_plate in people:
        db.audit("tailgate", ev_id, {"second_plate": read.second_plate})
    return result


def apply_plate(ev_id: str, lot_id: str, lane: str, plate: str, at: datetime, tailgate=False,
                confirmed_by: str | None = None) -> dict:
    """Attach a confirmed plate read to a session (open on entry, close + charge on exit)."""
    drv = db.one("SELECT * FROM drivers WHERE plate=? AND status='active'", plate)
    if not drv:
        db.update("events", ev_id, decision="not_enrolled", reason="driver no longer enrolled")
        return _event_out(ev_id)
    if confirmed_by:
        db.update("events", ev_id, decision="confirmed", review_state=f"confirmed:{confirmed_by}")

    if lane == "entry":
        dup = db.one("SELECT * FROM sessions WHERE plate=? AND lot_id=? AND state='active' ORDER BY entry_at DESC",
                     plate, lot_id)
        if dup and at - db.parse(dup["entry_at"]) < DUPLICATE_WINDOW:
            db.update("events", ev_id, session_id=dup["id"], reason="duplicate entry read: same pass, ignored")
            return _event_out(ev_id)
        if dup:  # we missed this vehicle's exit last time
            mark_missing_exit(dup["id"])
        sid = db.new_id("ses")
        flags = ["tailgate"] if tailgate else []
        # cloned / duplicate plate: the same plate is already parked in another lot right now
        elsewhere = db.q("SELECT id, lot_id FROM sessions WHERE plate=? AND lot_id<>? AND state='active'", plate, lot_id)
        if elsewhere:
            flags.append("clone_suspected")
            for o in elsewhere:
                _add_flag(o["id"], "clone_suspected")
            db.audit("clone_suspected", plate, {"lots": [lot_id] + [o["lot_id"] for o in elsewhere]})
        db.insert("sessions", {"id": sid, "lot_id": lot_id, "driver_id": drv["id"], "plate": plate,
                               "entry_at": db.iso(at), "entry_event_id": ev_id, "state": "active", "flags": flags,
                               "created_at": db.iso(db.now()), "updated_at": db.iso(db.now())})
        db.update("events", ev_id, session_id=sid)
        return _event_out(ev_id)

    # exit
    s = db.one("SELECT * FROM sessions WHERE plate=? AND lot_id=? AND state IN ('active','pending_confirm') "
               "ORDER BY entry_at DESC", plate, lot_id)
    if not s:
        recent = db.one("SELECT * FROM sessions WHERE plate=? AND lot_id=? AND exit_at IS NOT NULL "
                        "ORDER BY exit_at DESC", plate, lot_id)
        if recent and abs(at - db.parse(recent["exit_at"])) < DUPLICATE_WINDOW:
            # Duplicate exit read. Replaying the charge with the same PayPal-Request-Id proves no double charge.
            replay = None
            if recent["capture_id"]:
                replay = paypal.charge(recent["id"], drv["vault_id"], recent["amount_cents"], "replay")
            db.update("events", ev_id, session_id=recent["id"],
                      reason=f"duplicate exit read: idempotent replay returned capture "
                             f"{replay['capture_id'] if replay else '-'} (no new charge)")
            db.audit("duplicate_exit", recent["id"], {"replay": replay})
            return _event_out(ev_id)
        db.update("events", ev_id, decision="review", review_state="open",
                  reason="exit without a matching entry (missed entry read?)")
        return _event_out(ev_id)

    db.update("sessions", s["id"], exit_at=db.iso(at), exit_event_id=ev_id, updated_at=db.iso(db.now()))
    db.update("events", ev_id, session_id=s["id"])
    flags = _session_flags(s) + (["tailgate"] if tailgate else [])
    if tailgate and "tailgate" not in _session_flags(s):
        _add_flag(s["id"], "tailgate")
    # Same plate, different-looking car at exit (body colour): likely a cloned plate. Hold, don't charge.
    entry_ev = db.one("SELECT vehicle_color FROM events WHERE id=?", s["entry_event_id"]) or {}
    exit_ev = db.one("SELECT vehicle_color FROM events WHERE id=?", ev_id) or {}
    c_in, c_out = entry_ev.get("vehicle_color"), exit_ev.get("vehicle_color")
    if c_in and c_out and "unknown" not in (c_in, c_out) and c_in != c_out and not confirmed_by:
        _add_flag(s["id"], "vehicle_mismatch")
        flags.append("vehicle_mismatch")
        db.update("events", ev_id, reason=f"exit vehicle looks different from entry ({c_in} vs {c_out}): held, not charged")
        db.audit("vehicle_mismatch", s["id"], {"entry": c_in, "exit": c_out})
    if "clone_suspected" in flags or "vehicle_mismatch" in flags:
        quote_only(s["id"], at)
        db.update("sessions", s["id"], state="review")
        return _event_out(ev_id)
    charge_session(s["id"], at)
    return _event_out(ev_id)


def quote_only(session_id: str, exit_at: datetime) -> dict:
    s = db.one("SELECT * FROM sessions WHERE id=?", session_id)
    rule = rule_for(lot(s["lot_id"]))
    quote = price(rule, db.parse(s["entry_at"]), exit_at)
    db.update("sessions", session_id, amount_cents=quote.amount_cents, quote_json=quote.to_dict(),
              rule_json=rule.to_dict())
    return quote.to_dict()


def charge_session(session_id: str, exit_at: datetime, cap_cents: int | None = None) -> dict:
    s = db.one("SELECT * FROM sessions WHERE id=?", session_id)
    drv = db.one("SELECT * FROM drivers WHERE id=?", s["driver_id"])
    L = lot(s["lot_id"])
    quote = quote_only(session_id, exit_at)
    amount = quote["amount_cents"]
    if cap_cents is not None and amount > cap_cents:  # promised default (missing exit) is a ceiling
        amount = cap_cents
        quote = {**quote, "amount_cents": amount, "explanation": quote["explanation"] +
                 f"; capped at the ${cap_cents / 100:.2f} default for a missing exit read"}
        db.update("sessions", session_id, amount_cents=amount, quote_json=quote)
    if amount == 0:
        db.update("sessions", session_id, state="charged", capture_status="FREE", updated_at=db.iso(db.now()))
        return {"amount_cents": 0}
    desc = f"Parking at {L['name']}: {quote['minutes_total']} min"
    try:
        res = paypal.charge(session_id, drv["vault_id"], amount, desc)
    except Exception as e:  # never lose the session: park it in review with the error
        db.update("sessions", session_id, state="payment_failed", updated_at=db.iso(db.now()))
        db.audit("charge_failed", session_id, str(e))
        return {"error": str(e)}
    db.update("sessions", session_id, state="charged", order_id=res["order_id"], capture_id=res["capture_id"],
              capture_status=res["status"], updated_at=db.iso(db.now()))
    db.audit("charged", session_id, res)
    return res


def mark_missing_exit(session_id: str):
    """No exit read: ask the driver to confirm. Fair default = one day's cap (or 24h at the posted rate)."""
    s = db.one("SELECT * FROM sessions WHERE id=?", session_id)
    rule = rule_for(lot(s["lot_id"]))
    entry = db.parse(s["entry_at"])
    default = price(rule, entry, entry + MISSING_EXIT_AFTER)
    first_day_cap = rule.daily_cap_cents if rule.daily_cap_cents is not None else default.amount_cents
    amount = min(default.amount_cents, first_day_cap)
    db.update("sessions", session_id, state="pending_confirm", amount_cents=amount,
              quote_json={**default.to_dict(), "amount_cents": amount,
                          "explanation": "No exit read. Default capped at one day's maximum until you confirm "
                                         "your exit time."},
              rule_json=rule.to_dict(), updated_at=db.iso(db.now()))
    _add_flag(session_id, "missing_exit")
    db.audit("missing_exit", session_id, {"default_cents": amount})


def sweep_missing_exits(lot_id: str) -> list[str]:
    now = lot_now(lot_id)
    out = []
    for s in db.q("SELECT * FROM sessions WHERE lot_id=? AND state='active'", lot_id):
        if now - db.parse(s["entry_at"]) > MISSING_EXIT_AFTER:
            mark_missing_exit(s["id"])
            out.append(s["id"])
    return out


def confirm_exit(session_id: str, exit_at: datetime | None) -> dict:
    """Driver confirms their exit time for a session with no exit read (or accepts the default)."""
    s = db.one("SELECT * FROM sessions WHERE id=?", session_id)
    if s["state"] != "pending_confirm":
        raise ValueError("session is not waiting for confirmation")
    entry = db.parse(s["entry_at"])
    default_cents = s["amount_cents"]
    if exit_at is None:
        exit_at = entry + MISSING_EXIT_AFTER
    if exit_at < entry:
        raise ValueError("exit before entry")
    db.update("sessions", session_id, exit_at=db.iso(exit_at))
    # whatever the driver confirms, they never pay more than the default we showed them
    return charge_session(session_id, exit_at, cap_cents=default_cents)


def resolve_review(event_id: str, plate: str | None, by: str) -> dict:
    ev = db.one("SELECT * FROM events WHERE id=?", event_id)
    if not ev or ev["review_state"] != "open":
        raise ValueError("event is not open for review")
    if plate is None:
        db.update("events", event_id, review_state=f"rejected:{by}", decision="rejected")
        shutil.rmtree(config.EVIDENCE_DIR / event_id, ignore_errors=True)
        return _event_out(event_id)
    return apply_plate(event_id, ev["lot_id"], ev["lane"], vision.norm(plate), db.parse(ev["at"]), confirmed_by=by)


def purge_expired_evidence() -> int:
    """Delete evidence frames once the dispute window has passed (open disputes are kept)."""
    cutoff = db.iso(db.now() - timedelta(days=config.EVIDENCE_RETENTION_DAYS))
    n = 0
    for ev in db.q("SELECT e.id FROM events e LEFT JOIN sessions s ON s.id=e.session_id "
                   "WHERE e.created_at < ? AND e.evidence IS NOT NULL AND "
                   "(s.state IS NULL OR s.state NOT IN ('disputed','review'))", cutoff):
        shutil.rmtree(config.EVIDENCE_DIR / ev["id"], ignore_errors=True)
        db.update("events", ev["id"], evidence=None)
        n += 1
    return n


def _event_out(ev_id: str) -> dict:
    ev = db.one("SELECT * FROM events WHERE id=?", ev_id)
    ev["read_json"] = db.j(ev["read_json"])
    ev["evidence"] = db.j(ev["evidence"])
    if ev.get("session_id"):
        ev["session"] = session_out(ev["session_id"])
    return ev


def session_out(sid: str) -> dict | None:
    s = db.one("SELECT * FROM sessions WHERE id=?", sid)
    if not s:
        return None
    for k in ("quote_json", "rule_json", "dispute_json", "flags"):
        s[k] = db.j(s[k])
    return s
