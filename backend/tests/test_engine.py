"""End-to-end engine tests on the rendered demo clips (real plate reader, mock PayPal).

Each fraud / edge case from the PRD has a fixture clip: duplicate exit, cloned plate, tailgating,
missing exit, low-confidence read, non-enrolled vehicle.
"""
from datetime import timedelta

from driveoff import config, db, demo, disputes, engine, paypal, vision

LOT = "lot_main"


def play(clip, at=None):
    meta = next(c for c in demo.clip_list() if c["id"] == clip)
    return engine.process_pass(LOT, meta["lane"], demo.clip_frames(clip), at=at, source=f"clip:{clip}")


def t0():
    return engine.lot_now(LOT)


def test_entry_exit_charges_posted_rate(fresh):
    start = t0()
    e = play("maya_in", start)
    assert e["decision"] == "auto" and e["session"]["state"] == "active"
    x = play("maya_out", start + timedelta(hours=2))
    s = x["session"]
    assert s["state"] == "charged"
    assert s["amount_cents"] == 600  # 120 min - 30 free = 90 -> 2 started hours x $3
    assert s["capture_id"].startswith("MOCKC")


def test_duplicate_exit_read_never_double_charges(fresh):
    start = t0()
    play("maya_in", start)
    first = play("maya_out", start + timedelta(hours=3))
    again = play("maya_out_again", start + timedelta(hours=3, minutes=1))
    assert again["session_id"] == first["session_id"]
    assert "idempotent replay" in again["reason"]
    charges = [c for c in paypal.MOCK.calls if c[0] == "charge"]
    assert len(charges) == 2 and charges[0][1] == charges[1][1]  # same PayPal-Request-Id
    assert len(paypal.MOCK.captures) == 1  # one capture only


def test_unenrolled_vehicle_is_hashed_and_dropped(fresh):
    e = play("stranger_in")
    assert e["decision"] == "not_enrolled"
    assert e["plate"] is None and e["evidence"] is None
    assert not (config.EVIDENCE_DIR / e["id"]).exists()
    assert db.one("SELECT * FROM anon_presence")["plate_hash"] == vision.plate_hash("PWN6613")


def test_low_confidence_exit_goes_to_review_not_charged(fresh):
    start = t0()
    play("sam_in", start)
    x = play("sam_out_night", start + timedelta(hours=1))
    assert x["decision"] == "review" and x["review_state"] == "open"
    s = db.one("SELECT * FROM sessions WHERE plate='BXD7390'")
    assert s["state"] == "active" and s["capture_id"] is None
    # operator confirms the plate -> charged
    done = engine.resolve_review(x["id"], "BXD7390", "operator")
    assert done["session"]["state"] == "charged"


def test_cloned_plate_different_car_is_held(fresh):
    start = t0()
    play("ana_in", start)  # yellow car
    x = play("clone_out", start + timedelta(hours=2))  # green car, same plate
    assert x["session"]["state"] == "review"
    assert "vehicle_mismatch" in x["session"]["flags"]
    assert x["session"]["capture_id"] is None


def test_same_plate_in_two_lots_is_flagged(fresh):
    play("maya_in")
    frames = demo.clip_frames("maya_in")
    e = engine.process_pass("lot_mall", "entry", frames, source="test")
    assert "clone_suspected" in e["session"]["flags"]
    other = db.one("SELECT flags FROM sessions WHERE lot_id='lot_main'")
    assert "clone_suspected" in db.j(other["flags"])


def test_tailgating_pass_detects_both_vehicles(fresh):
    e = play("tailgate_in")
    both = [e, e["tailgate_with"]]
    decisions = sorted(x["decision"] for x in both)
    assert decisions == ["auto", "not_enrolled"]
    leo = next(x for x in both if x["decision"] == "auto")
    assert leo["plate"] == "JNP3047"
    assert "tailgate" in leo["session"]["flags"]
    assert "tailgating" in leo["reason"]


def test_missing_exit_defaults_to_one_day_cap_then_driver_confirms(fresh):
    start = t0()
    play("ana_in", start)
    demo.advance_clock(LOT, 60 * 25)
    swept = engine.sweep_missing_exits(LOT)
    assert len(swept) == 1
    s = engine.session_out(swept[0])
    assert s["state"] == "pending_confirm" and s["amount_cents"] == 2000
    entry = db.parse(s["entry_at"])
    engine.confirm_exit(s["id"], entry + timedelta(hours=1))
    s = engine.session_out(s["id"])
    assert s["state"] == "charged" and s["amount_cents"] == 300


def test_wrong_time_dispute_finds_real_exit_and_partially_refunds(fresh):
    start = t0()
    play("sam_in", start)
    play("sam_out_night", start + timedelta(minutes=50))  # real exit, low confidence -> review, unresolved
    demo.advance_clock(LOT, 60 * 25)
    sid = engine.sweep_missing_exits(LOT)[0]
    engine.confirm_exit(sid, None)  # no reply: charged the capped default
    s = engine.session_out(sid)
    assert s["amount_cents"] == 2000
    verdict = disputes.open_dispute(sid, "wrong_time", "I left before 10")
    assert verdict["outcome"] == "partial_refund"
    assert verdict["fair_cents"] == 300 and verdict["refund_cents"] == 1700
    assert engine.session_out(sid)["state"] == "refunded"


def test_strong_evidence_dispute_goes_to_operator(fresh):
    start = t0()
    play("maya_in", start)
    x = play("maya_out", start + timedelta(hours=2))
    v = disputes.open_dispute(x["session_id"], "not_me")
    assert v["outcome"] == "to_operator" and v["refund_cents"] == 0


def test_correction_and_decision_rules():
    assert vision.correct("1SZ0522", "LLLDDDD") == "ISZ0522"
    assert vision.correct("7YK934O", "LLLDDDD") == "TYK9340"
    r = vision.PassRead("KTR4821", 0.99, 5, 6, {"KTR4821": {}}, [])
    assert vision.decide(r, {"KTR4821"}, "LLLDDDD").outcome == "auto"
    assert vision.decide(r, {"KTR4821", "KTR4824"}, "LLLDDDD").outcome == "review"  # near-collision
    weak = vision.PassRead("KTR4821", 0.80, 5, 6, {"KTR4821": {}}, [])
    assert vision.decide(weak, {"KTR4821"}, "LLLDDDD").outcome == "review"
    few = vision.PassRead("KTR4821", 0.99, 2, 6, {"KTR4821": {}}, [])
    assert vision.decide(few, {"KTR4821"}, "LLLDDDD").outcome == "review"
    off = vision.PassRead("KTR4828", 0.99, 6, 6, {"KTR4828": {}}, [])
    assert vision.decide(off, {"KTR4821"}, "LLLDDDD").outcome == "review"  # 1 char from enrolled: never auto


def test_vehicle_colour_signal_on_fixture_clips():
    for clip, colour in [("maya_in", "red"), ("sam_in", "blue"), ("clone_out", "green"), ("ana_in", "yellow")]:
        frames = demo.clip_frames(clip)
        r = vision.read_pass(frames, "LLLDDDD")
        box = next(x for x in r.reads if x.frame == r.best_frame).box
        assert vision.vehicle_color(frames[r.best_frame], box) == colour, clip
