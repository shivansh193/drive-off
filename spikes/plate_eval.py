"""Spike 3: run the open plate stack (fast-alpr: YOLO plate detector + fast-plate-ocr) on a labelled set.

Reports exact-match and per-character accuracy, accuracy by condition, and confidence calibration.

  python spikes/plate_eval.py spikes/out/synth
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from fast_alpr import ALPR

DETECTOR = "yolo-v9-t-384-license-plate-end2end"
OCR = "cct-xs-v1-global-model"


def norm(s):
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def char_acc(pred, truth):
    # position-wise match over the longer string (penalises missing / extra chars)
    n = max(len(pred), len(truth)) or 1
    return sum(1 for a, b in zip(pred, truth) if a == b) / n


def ocr_conf(ocr):
    c = getattr(ocr, "confidence", None)
    if c is None:
        return 0.0
    if isinstance(c, (list, tuple, np.ndarray)):
        return float(np.mean(c)) if len(c) else 0.0
    return float(c)


def main(folder):
    folder = Path(folder)
    truth = json.loads((folder / "truth.json").read_text())
    alpr = ALPR(detector_model=DETECTOR, ocr_model=OCR)
    rows = []
    t0 = time.time()
    for t in truth:
        frame = cv2.imread(str(folder / t["file"]))
        res = alpr.predict(frame)
        if res:
            best = max(res, key=lambda r: r.detection.confidence)
            pred = norm(best.ocr.text if best.ocr else "")
            det_c = float(best.detection.confidence)
            oc = ocr_conf(best.ocr) if best.ocr else 0.0
        else:
            pred, det_c, oc = "", 0.0, 0.0
        gt = norm(t["plate"])
        rows.append({**t, "pred": pred, "det_conf": round(det_c, 3), "ocr_conf": round(oc, 3),
                     "exact": pred == gt, "char_acc": round(char_acc(pred, gt), 3), "detected": bool(res)})
    ms = (time.time() - t0) * 1000 / max(1, len(rows))

    by = defaultdict(list)
    for r in rows:
        by[r["condition"]].append(r)
    print(f"images: {len(rows)}   avg latency: {ms:.0f} ms/frame (CPU)")
    print(f"detected: {sum(r['detected'] for r in rows)}/{len(rows)}")
    print(f"exact match: {np.mean([r['exact'] for r in rows]):.1%}   per-char: {np.mean([r['char_acc'] for r in rows]):.1%}")
    print("\nby condition:")
    for c, rs in sorted(by.items()):
        print(f"  {c:11s} n={len(rs):3d}  exact={np.mean([r['exact'] for r in rs]):6.1%}  char={np.mean([r['char_acc'] for r in rs]):6.1%}")
    print("\ncalibration (OCR confidence bin -> exact-match rate):")
    for lo, hi in [(0, .5), (.5, .7), (.7, .85), (.85, .95), (.95, 1.01)]:
        rs = [r for r in rows if lo <= r["ocr_conf"] < hi]
        if rs:
            print(f"  [{lo:.2f},{min(hi,1):.2f})  n={len(rs):3d}  exact={np.mean([r['exact'] for r in rs]):6.1%}")
    for thr in (0.8, 0.9, 0.95):
        auto = [r for r in rows if r["ocr_conf"] >= thr]
        wrong = sum(not r["exact"] for r in auto)
        print(f"  threshold {thr:.2f}: auto={len(auto)}/{len(rows)} wrong-auto={wrong} review-queue={len(rows)-len(auto)}")
    out = folder / "eval_results.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nper-image results: {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "spikes/out/synth")
