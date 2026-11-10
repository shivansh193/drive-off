"""Driver disputes: 'Not me' / 'Wrong time'. AI reviews the evidence; refunds automatically only when it supports the driver.

Signals, all recorded on the session so the operator and driver can see why:
  1. colour   : body colour at entry vs exit (deterministic, OpenCV)
  2. vision   : Gemini compares the entry and exit evidence frames ("same vehicle?")
  3. reads    : plate-read confidence and frame agreement at entry and exit
  4. timeline : review-queue reads of this plate (low confidence) between entry and the charged exit,
                which may be the driver's real exit
  5. flags    : clone_suspected / tailgate on the session

Policy:
  * different vehicle (vision says so with conf >= 0.8, or colours differ and vision doesn't contradict)
      - and an earlier low-confidence exit read matches the entry vehicle -> partial refund down to the fair amount
      - otherwise -> full refund; plate flagged as possibly cloned
  * same vehicle with strong reads -> operator decides (no automatic refund)
"""
from __future__ import annotations

from datetime import datetime

from . import config, db, engine, gemini, paypal
from .rates import RateRule, price
from zoneinfo import ZoneInfo


def _local(iso: str, tz: str) -> str:
    d = db.parse(iso).astimezone(ZoneInfo(tz))
    return d.strftime("%b %d, ") + d.strftime("%I:%M %p").lstrip("0")


def _img(event_id: str | None, name="frame.jpg") -> bytes | None:
    if not event_id:
        return None
    p = config.EVIDENCE_DIR / event_id / name
    return p.read_bytes() if p.exists() else None


def _vision_compare(entry_ev: str, exit_ev: str) -> dict:
    a, b = _img(entry_ev), _img(exit_ev)
    if not (a and b):
        return {"error": "evidence frames missing"}
    prompt = ("Image 1 is a parking-lot ENTRY camera frame, image 2 is the EXIT camera frame for the same billed "
              "parking session. Faces, if any, are blurred; ignore people entirely. Compare only the vehicles: body "
              "colour, shape, lights, bumper, and the plate. Are they the same vehicle? Be conservative: say "
              "same_vehicle=false only if there is a clear visible difference.")
    try:
        return gemini.generate_json(prompt, [a, b], '{"same_vehicle": bool, "confidence": float 0-1, '
                                                    '"differences": [str], "summary": str}')
    except gemini.GeminiUnavailable as e:
        return {"error": str(e)}


def open_dispute(session_id: str, reason: str, note: str = "", claimed_exit: datetime | None = None) -> dict:
    s = engine.session_out(session_id)
    if not s or s["state"] not in ("charged", "review"):
        raise ValueError("only charged sessions can be disputed")
    if s.get("dispute_json"):
        return s["dispute_json"]
    entry = db.one("SELECT * FROM events WHERE id=?", s["entry_event_id"])
    exit_ = db.one("SELECT * FROM events WHERE id=?", s["exit_event_id"])
    signals: dict = {"reason": reason, "note": note}

    signals["colour"] = {"entry": entry and entry["vehicle_color"], "exit": exit_ and exit_["vehicle_color"]}
    colour_differs = (signals["colour"]["entry"] not in (None, "unknown") and signals["colour"]["exit"] not in
                      (None, "unknown") and signals["colour"]["entry"] != signals["colour"]["exit"])
    signals["vision"] = _vision_compare(s["entry_event_id"], s["exit_event_id"])
    v = signals["vision"]
    vision_diff = v.get("same_vehicle") is False and float(v.get("confidence", 0)) >= 0.8
    vision_same = v.get("same_vehicle") is True and float(v.get("confidence", 0)) >= 0.8
    signals["reads"] = {"entry": entry and {"confidence": entry["confidence"], "agree": entry["agree"]},
                        "exit": exit_ and {"confidence": exit_["confidence"], "agree": exit_["agree"]}}
    signals["flags"] = s["flags"] or []

    # earlier low-confidence exit reads of (approximately) this plate, matching the entry vehicle's colour
    candidates = []
    for ev in db.q("SELECT * FROM events WHERE lot_id=? AND lane='exit' AND at>? AND at<? AND decision='review' "
                   "ORDER BY at", s["lot_id"], s["entry_at"], s["exit_at"]):
        rj = db.j(ev["read_json"], {}) or {}
        llm = rj.get("llm") or {}
        names = set(rj.get("candidates", {})) | {ev["plate"], (llm.get("plate") or "").upper().replace(" ", "")}
        from .vision import edit_distance
        if any(n and edit_distance(n, s["plate"]) <= 1 for n in names):
            candidates.append({"event_id": ev["id"], "at": ev["at"], "confidence": ev["confidence"],
                               "colour": ev["vehicle_color"], "llm_plate": llm.get("plate"),
                               "matches_entry_colour": ev["vehicle_color"] == signals["colour"]["entry"]})
    if claimed_exit:
        signals["claimed_exit"] = db.iso(claimed_exit)
    signals["earlier_exit_candidates"] = candidates

    different_vehicle = exit_ is not None and (vision_diff or (colour_differs and not vision_same))
    verdict: dict = {"signals": signals, "opened_at": db.iso(db.now())}
    rule = RateRule.from_dict(s["rule_json"])
    true_exit = next((c for c in candidates if c["matches_entry_colour"]), None)
    if true_exit and (different_vehicle or exit_ is None or reason == "wrong_time"):
        fair = price(rule, db.parse(s["entry_at"]), db.parse(true_exit["at"]))
        refund_cents = max(0, s["amount_cents"] - fair.amount_cents)
        why = ("there was no clean exit read, so you were charged the capped default" if exit_ is None else
               f"the exit vehicle doesn't match the entry vehicle ({signals['colour']['entry']} vs "
               f"{signals['colour']['exit']})" if different_vehicle else "you disputed the exit time")
        verdict.update(outcome="partial_refund", fair_cents=fair.amount_cents, refund_cents=refund_cents,
                       explanation=(f"Because {why}, we checked for other exit reads. A low-confidence exit read at "
                                    f"{_local(true_exit['at'], rule.timezone)} matches your car ({true_exit['colour']}"
                                    f"{', AI read ' + true_exit['llm_plate'] if true_exit.get('llm_plate') else ''}). "
                                    f"Your stay was re-priced to end then: ${fair.amount_cents / 100:.2f}, so "
                                    f"${refund_cents / 100:.2f} goes back to your PayPal."))
    elif different_vehicle:
        refund_cents = s["amount_cents"]
        verdict.update(outcome="full_refund", refund_cents=refund_cents,
                       explanation="The exit vehicle doesn't match the entry vehicle, so this charge is refunded in full.")
        engine._add_flag(session_id, "clone_suspected")
    else:
        verdict.update(outcome="to_operator", refund_cents=0,
                       explanation="The evidence shows the same vehicle at entry and exit with strong plate reads, "
                                   "so the lot operator will review your dispute.")

    if verdict["refund_cents"] > 0 and s.get("capture_id"):
        res = paypal.refund(s["capture_id"], verdict["refund_cents"], f"refund-{session_id}",
                            f"Drive-Off dispute upheld: {verdict['outcome'].replace('_', ' ')}")
        verdict["paypal_refund"] = res
        db.update("sessions", session_id, refund_id=res["refund_id"], refund_cents=verdict["refund_cents"],
                  state="refunded")
    else:
        db.update("sessions", session_id, state="disputed")
    db.update("sessions", session_id, dispute_json=verdict, updated_at=db.iso(db.now()))
    db.audit("dispute", session_id, {"outcome": verdict["outcome"], "refund_cents": verdict["refund_cents"]})
    return verdict


def operator_decide(session_id: str, refund: bool, note: str = "") -> dict:
    s = engine.session_out(session_id)
    verdict = s.get("dispute_json") or {}
    if refund and s.get("capture_id") and not s.get("refund_id"):
        res = paypal.refund(s["capture_id"], s["amount_cents"], f"refund-{session_id}", "Refunded by the lot operator")
        db.update("sessions", session_id, refund_id=res["refund_id"], refund_cents=s["amount_cents"], state="refunded")
        verdict["paypal_refund"] = res
    else:
        db.update("sessions", session_id, state="charged")
    verdict["operator"] = {"refund": refund, "note": note, "at": db.iso(db.now())}
    db.update("sessions", session_id, dispute_json=verdict)
    return verdict
