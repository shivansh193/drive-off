"""Evaluation for the README and the in-app Eval page. Writes docs/eval.json.

1. Single-frame plate reads on a held-out rendered set (different seed from the spike), by condition.
2. Pass-level auto-charge safety: simulated multi-frame passes through the real decision rules against an
   enrolled set that deliberately includes one-character-apart plate pairs and look-alike strangers.
3. Test-suite summary (pytest).

  .venv/Scripts/python scripts/run_eval.py
"""
import json
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "spikes"))

import synth_plates as sp  # noqa: E402
from driveoff import config, demo, vision  # noqa: E402

SEED = 2026
FMT = "LLLDDDD"


def single_frame(n=120):
    rng = random.Random(SEED)
    rows = []
    for i in range(n):
        cond = sp.CONDITIONS[i % len(sp.CONDITIONS)]
        text = sp.plate_text(rng, confusable=(cond == "confusable"))
        car, _ = sp.render_car(rng, sp.render_plate(text))
        img = np.array(sp.degrade(car, cond, rng))[:, :, ::-1].copy()
        reads = vision.read_frame(img, 0, None)  # raw model output: no format correction
        best = max(reads, key=lambda r: r.det_conf) if reads else None
        pred = best.raw if best else ""
        n_ = max(len(pred), len(text)) or 1
        rows.append({"cond": cond, "exact": pred == text, "detected": bool(best),
                     "char": sum(a == b for a, b in zip(pred, text)) / n_, "conf": best.ocr_conf if best else 0.0})
    by = defaultdict(list)
    for r in rows:
        by[r["cond"]].append(r)
    agg = lambda rs: {"n": len(rs), "exact": float(np.mean([r["exact"] for r in rs])),  # noqa: E731
                      "char": float(np.mean([r["char"] for r in rs])),
                      "detected": float(np.mean([r["detected"] for r in rs]))}
    cal = []
    for lo, hi in [(0, .5), (.5, .8), (.8, .9), (.9, .95), (.95, 1.01)]:
        rs = [r for r in rows if lo <= r["conf"] < hi]
        if rs:
            cal.append({"bin": f"[{lo:.2f}, {min(hi, 1):.2f})", "n": len(rs), "exact": float(np.mean([r["exact"] for r in rs]))})
    return {**agg(rows), "by_condition": {c: agg(rs) for c, rs in sorted(by.items())}, "calibration": cal}


def near_variant(p, rng):
    i = rng.randrange(len(p))
    pool = sp.LETTERS if p[i].isalpha() else sp.DIGITS
    return p[:i] + rng.choice([c for c in pool if c != p[i]]) + p[i + 1:]


def passes(n=150, frames=5):
    rng = random.Random(SEED + 1)
    enrolled = [sp.plate_text(rng) for _ in range(40)]
    near_pairs = 10
    enrolled += [near_variant(p, rng) for p in enrolled[:near_pairs]]
    enrolled_set = set(enrolled)
    conds = ["day", "night", "angle", "blur", "dirty"]
    outcomes = Counter()
    by = defaultdict(lambda: Counter())
    wrong = 0
    auto_ok = 0
    twins = {p for p in enrolled for q in enrolled if p != q and vision.edit_distance(p, q) <= 1}
    kinds = defaultdict(Counter)
    reasons = Counter()
    for i in range(n):
        kind = rng.random()
        if kind < 0.6:
            truth = rng.choice(enrolled)
        elif kind < 0.8:
            truth = near_variant(rng.choice(enrolled), rng)  # stranger one char from a customer
            if truth in enrolled_set:
                truth = sp.plate_text(rng)
        else:
            truth = sp.plate_text(rng)
        cond = conds[i % len(conds)]
        color = rng.choice(list(demo.COLORS))
        imgs = []
        for f in range(frames):
            fr = demo.render_frame([(truth, color)], f / (frames - 1), rng)
            pil = sp.Image.fromarray(fr[:, :, ::-1])
            imgs.append(np.array(sp.degrade(pil, cond, rng))[:, :, ::-1].copy())
        read = vision.read_pass(imgs, FMT)
        dec = vision.decide(read, enrolled_set, FMT)
        o = dec.outcome
        outcomes[o] += 1
        k = ("customer" if truth not in twins else "customer_with_lookalike_customer") if truth in enrolled_set else (
            "stranger_1_char_from_customer" if any(vision.edit_distance(truth, e) <= 1 for e in enrolled_set) else "stranger")
        kinds[k][o] += 1
        if o == "review":
            for r in dec.reasons:
                reasons[r.split(" (")[0].split(" is 1")[0].rstrip("0123456789. ")[:60]] += 1
        by[cond][o] += 1
        if o == "auto":
            if dec.plate != truth:
                wrong += 1
                by[cond]["wrong"] += 1
            else:
                auto_ok += 1
    autos = outcomes["auto"]
    return {
        "n": n, "frames_per_pass": frames, "enrolled": len(enrolled), "near_pairs": near_pairs,
        "min_agree": config.AUTO_MIN_AGREE, "min_conf": config.AUTO_MIN_CONF,
        "outcomes": dict(outcomes), "wrong_person_charges": wrong,
        "auto_precision": (auto_ok / autos) if autos else None, "auto_rate": autos / n,
        "review_rate": outcomes["review"] / n,
        "by_vehicle_kind": {k: dict(v) for k, v in kinds.items()},
        "review_reasons": dict(reasons.most_common(8)),
        "by_condition": {c: {"auto": v["auto"] / sum(v[k] for k in v if k != "wrong"),
                             "review": v["review"] / sum(v[k] for k in v if k != "wrong"), "wrong": v["wrong"]}
                         for c, v in by.items()},
    }


def tests():
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "backend/tests"], cwd=ROOT, capture_output=True, text=True)
    last = [l for l in r.stdout.strip().splitlines() if "passed" in l or "failed" in l]
    return {"summary": (last[-1] if last else r.stdout[-200:]) + " (rate-engine property tests incl. caps, grace, "
            "free windows, midnight rollover, DST; idempotent duplicate exit; clone, tailgate, missing exit, "
            "low-confidence and dispute fixtures)", "ok": r.returncode == 0}


if __name__ == "__main__":
    t0 = time.time()
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seed": SEED,
        "note": "Rendered, fictional plates only. This measures the pipeline and the safety rules, not a real lot.",
        "single_frame": single_frame(),
        "passes": passes(),
        "tests": tests(),
        "limits": [
            "All images are synthetic renders, not real lot footage. Real-world accuracy will be lower, especially for "
            "glare, plate frames, regional formats and mud.",
            "Plate format is a single US-style pattern (LLLDDDD) per lot.",
            "Vehicle-colour matching is a coarse signal and is only used to hold or dispute, never to charge.",
            "Gemini second pass is advisory: it never triggers a charge on its own.",
        ],
    }
    (ROOT / "docs" / "eval.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in ("single_frame", "passes")}, indent=1)[:3000])
    print(f"done in {time.time() - t0:.0f}s")
