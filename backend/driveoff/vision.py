"""Plate reading for one pass of a vehicle through a lane.

frames -> plate detector (YOLOv9-t) -> OCR (fast-plate-ocr) per frame
       -> format-aware correction (letter vs digit positions)
       -> multi-frame vote
       -> decision against the enrolled plates (auto / review / not enrolled)

Safety rules for an automatic charge (docs/SPIKES.md, spike 3):
  * >= AUTO_MIN_AGREE frames agree on the same corrected plate
  * mean OCR confidence of the agreeing frames >= AUTO_MIN_CONF
  * the plate exactly matches an enrolled plate
  * no *other* enrolled plate is within edit distance 1 of the read or of any minority candidate
Anything else that could be an enrolled driver goes to review (Gemini second pass + human/driver confirmation).
Plates that are not enrolled and not near an enrolled plate are hashed for occupancy and dropped.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

from . import config, gemini

_alpr = None
_alpr_lock = threading.Lock()

LETTER_FIX = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "7": "T", "4": "A"}
DIGIT_FIX = {v: k for k, v in LETTER_FIX.items()} | {"D": "0", "Q": "0", "U": "0", "L": "1"}


def alpr():
    global _alpr
    with _alpr_lock:
        if _alpr is None:
            from fast_alpr import ALPR
            _alpr = ALPR(detector_model="yolo-v9-t-384-license-plate-end2end", ocr_model="cct-xs-v1-global-model")
        return _alpr


def norm(s: str | None) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def plate_hash(plate: str) -> str:
    return hmac.new(config.PLATE_HASH_SALT.encode(), norm(plate).encode(), hashlib.sha256).hexdigest()[:16]


def correct(text: str, fmt: str | None) -> str:
    """Apply a lot's plate pattern, e.g. 'LLLDDDD' (L = letter, D = digit, * = either)."""
    t = norm(text)
    if not fmt or len(t) != len(fmt):
        return t
    out = []
    for ch, f in zip(t, fmt):
        if f == "L" and ch.isdigit():
            ch = LETTER_FIX.get(ch, ch)
        elif f == "D" and ch.isalpha():
            ch = DIGIT_FIX.get(ch, ch)
        out.append(ch)
    return "".join(out)


def fits(text: str, fmt: str | None) -> bool:
    if not fmt:
        return True
    return len(text) == len(fmt) and all(
        f == "*" or (f == "L" and c.isalpha()) or (f == "D" and c.isdigit()) for c, f in zip(text, fmt))


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass
class FrameRead:
    frame: int
    box: list[int]
    det_conf: float
    raw: str
    text: str
    ocr_conf: float


@dataclass
class PassRead:
    plate: str
    confidence: float
    agree: int
    frames: int
    candidates: dict
    reads: list[FrameRead]
    multi_vehicle: bool = False
    second_plate: str | None = None
    best_frame: int | None = None
    llm: dict | None = None

    def to_dict(self):
        d = asdict(self)
        return d


def read_frame(img: np.ndarray, idx: int, fmt: str | None) -> list[FrameRead]:
    out = []
    model = alpr()
    with _alpr_lock:  # the ONNX sessions are shared; serialise inference
        results = model.predict(img)
    for r in results:
        if not r.ocr:
            continue
        c = r.ocr.confidence
        conf = float(np.mean(c)) if isinstance(c, (list, tuple, np.ndarray)) else float(c or 0)
        b = r.detection.bounding_box
        raw = norm(r.ocr.text)
        out.append(FrameRead(idx, [int(b.x1), int(b.y1), int(b.x2), int(b.y2)], float(r.detection.confidence),
                             raw, correct(raw, fmt), round(conf, 4)))
    return out


def read_pass(frames: list[np.ndarray], fmt: str | None) -> PassRead:
    reads: list[FrameRead] = []
    per_frame_count = []
    for i, f in enumerate(frames):
        rs = read_frame(f, i, fmt)
        per_frame_count.append(len(rs))
        reads.extend(rs)
    if not reads:
        return PassRead("", 0.0, 0, len(frames), {}, [])
    votes = Counter(r.text for r in reads if r.text)
    conf_by = defaultdict(list)
    for r in reads:
        conf_by[r.text].append(r.ocr_conf)
    plate, agree = votes.most_common(1)[0]
    mean_conf = float(np.mean(conf_by[plate]))
    # tailgating: two distinct plates in the same frame, in at least 2 frames
    multi = sum(1 for n in per_frame_count if n >= 2) >= 2
    second = None
    if multi:
        others = [(t, n) for t, n in votes.most_common() if t != plate and edit_distance(t, plate) > 2]
        second = others[0][0] if others else None
    best = max((r for r in reads if r.text == plate), key=lambda r: r.ocr_conf * (r.box[2] - r.box[0]))
    return PassRead(plate, round(mean_conf, 4), agree, len(frames),
                    {t: {"votes": n, "mean_conf": round(float(np.mean(conf_by[t])), 3)} for t, n in votes.items()},
                    reads, multi, second, best.frame)


@dataclass
class Decision:
    outcome: str  # auto | review | not_enrolled | no_read
    plate: str
    driver_plate: str | None
    reasons: list[str] = field(default_factory=list)


def decide(read: PassRead, enrolled: set[str], fmt: str | None) -> Decision:
    if not read.plate:
        return Decision("no_read", "", None, ["no plate detected"])
    reasons = []
    near = sorted(p for p in enrolled if edit_distance(p, read.plate) <= 1)
    for cand in read.candidates:
        near += [p for p in enrolled if edit_distance(p, cand) <= 1 and p not in near]
    exact = read.plate in enrolled
    if not exact and not near:
        return Decision("not_enrolled", read.plate, None, ["plate not enrolled: hashed for occupancy, image dropped"])
    if not exact:
        reasons.append(f"read {read.plate} is 1 character from enrolled plate(s) {', '.join(sorted(set(near)))}")
    if exact and len(set(near) - {read.plate}) > 0:
        reasons.append(f"another enrolled plate is 1 character away ({', '.join(sorted(set(near) - {read.plate}))})")
    if read.agree < config.AUTO_MIN_AGREE:
        reasons.append(f"only {read.agree} frame(s) agree (need {config.AUTO_MIN_AGREE})")
    if read.confidence < config.AUTO_MIN_CONF:
        reasons.append(f"confidence {read.confidence:.2f} < {config.AUTO_MIN_CONF:.2f}")
    if not fits(read.plate, fmt):
        reasons.append(f"read does not fit the lot's plate format {fmt}")
    if reasons:
        target = read.plate if exact else (sorted(set(near))[0] if len(set(near)) == 1 else None)
        return Decision("review", read.plate, target, reasons)
    return Decision("auto", read.plate, read.plate,
                    [f"{read.agree}/{read.frames} frames agree, confidence {read.confidence:.2f}, exact enrolled match"])


# ------------------------------------------------------------------ second pass + evidence
def crop(img: np.ndarray, box: list[int], pad=0.25) -> np.ndarray:
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    x1, y1 = max(0, int(x1 - w * pad)), max(0, int(y1 - h * pad))
    x2, y2 = min(img.shape[1], int(x2 + w * pad)), min(img.shape[0], int(y2 + h * pad))
    return img[y1:y2, x1:x2]


def jpeg(img: np.ndarray, q=85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes()


def llm_second_pass(crops: list[np.ndarray], candidates: dict, fmt: str | None) -> dict:
    crops = [c for c in crops if c is not None and c.size]
    if not crops:
        return {"skipped": "no plate crops to read"}
    prompt = (
        "These are crops of the same vehicle licence plate from consecutive camera frames at a parking lot lane. "
        f"An OCR model produced these candidate readings with vote counts: {candidates}. "
        f"The lot's plate format is {fmt or 'unknown'} (L=letter, D=digit). "
        "Read the plate yourself. Pay special attention to look-alike characters (0/O, 1/I, 8/B, 5/S, 2/Z). "
        "If any character is genuinely unreadable, say so and do not guess.")
    try:
        return gemini.generate_json(prompt, [jpeg(c) for c in crops[:4]],
                                    '{"plate": str, "confidence": float 0-1, "unreadable_positions": [int], "notes": str}')
    except gemini.GeminiUnavailable as e:
        return {"error": str(e)}


_face = None


def mask_faces(img: np.ndarray) -> tuple[np.ndarray, int]:
    """Blur any faces before an evidence frame is stored. No face recognition is ever run."""
    global _face
    if _face is None:
        _face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = _face.detectMultiScale(gray, 1.1, 5, minSize=(24, 24))
    out = img.copy()
    for (x, y, w, h) in faces:
        out[y:y + h, x:x + w] = cv2.GaussianBlur(out[y:y + h, x:x + w], (51, 51), 30)
    return out, len(faces)


def vehicle_color(img: np.ndarray, plate_box: list[int]) -> str:
    """Dominant body colour from the region above the plate (a cheap 'same vehicle?' signal for disputes)."""
    x1, y1, x2, y2 = plate_box
    w, h = x2 - x1, y2 - y1
    region = img[max(0, y1 - 3 * h):max(1, y1 - h // 2), max(0, x1 - w):min(img.shape[1], x2 + w)]
    if region.size == 0:
        return "unknown"
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    hch, s, v = np.median(hsv[:, 0]), np.median(hsv[:, 1]), np.median(hsv[:, 2])
    if v < 50:
        return "black"
    if s < 40:
        return "white" if v > 180 else "grey"
    for name, lo, hi in (("red", 0, 8), ("orange", 8, 22), ("yellow", 22, 35), ("green", 35, 85),
                         ("blue", 85, 130), ("purple", 130, 160), ("red", 160, 181)):
        if lo <= hch < hi:
            return name
    return "unknown"


def draw(img: np.ndarray, reads: list[FrameRead]) -> np.ndarray:
    out = img.copy()
    for r in reads:
        cv2.rectangle(out, (r.box[0], r.box[1]), (r.box[2], r.box[3]), (60, 220, 120), 3)
        cv2.putText(out, f"{r.text} {r.ocr_conf:.2f}", (r.box[0], max(20, r.box[1] - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (60, 220, 120), 2)
    return out
