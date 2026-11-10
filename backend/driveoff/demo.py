"""Demo lot, fictional drivers and rendered camera clips.

Every car and plate here is synthetic and fictional ("DEMO STATE / FICTIONAL"). No real plates, no real people.
"""
from __future__ import annotations

import json
import random
import shutil
from datetime import timedelta

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import config, db
from .rates import RateRule

COLORS = {"red": (200, 30, 35), "blue": (30, 70, 190), "white": (235, 235, 230), "green": (30, 150, 60),
          "yellow": (230, 190, 30), "black": (25, 25, 28), "grey": (128, 130, 135)}

DEMO_SIGN = "First 30 min free, then $3/hr, max $20/day."
DEMO_RULE = RateRule(rate_per_hour_cents=300, free_minutes=30, increment_minutes=60, daily_cap_cents=2000,
                     free_windows=[], timezone="America/Los_Angeles", sign_text=DEMO_SIGN)

DRIVERS = [
    {"name": "Maya (demo)", "plate": "KTR4821", "color": "red"},
    {"name": "Sam (demo)", "plate": "BXD7390", "color": "blue"},
    {"name": "Ana (demo)", "plate": "HLM2056", "color": "yellow"},
    {"name": "Leo (demo)", "plate": "JNP3047", "color": "white"},
]

# id, title, lane, plates+colours, condition, story text
CLIPS = [
    ("maya_in", "Maya arrives", "entry", [("KTR4821", "red")], "day", "Enrolled driver, daylight"),
    ("sam_in", "Sam arrives", "entry", [("BXD7390", "blue")], "day", "Enrolled driver, daylight"),
    ("stranger_in", "Visitor arrives (not enrolled)", "entry", [("PWN6613", "grey")], "day",
     "Not enrolled: plate hashed for occupancy, image dropped"),
    ("ana_in", "Ana arrives at an angle", "entry", [("HLM2056", "yellow")], "angle", "Steep camera angle"),
    ("maya_out", "Maya leaves", "exit", [("KTR4821", "red")], "day", "Exit: posted rate applied, PayPal charged"),
    ("maya_out_again", "Camera re-reads Maya", "exit", [("KTR4821", "red")], "day",
     "Duplicate exit read: idempotent, no double charge"),
    ("sam_out_night", "Sam leaves at night, plate smeared", "exit", [("BXD7390", "blue")], "night_blur",
     "Low confidence: never auto-charged, goes to review"),
    ("clone_out", "Green car with Ana's plate leaves", "exit", [("HLM2056", "green")], "day",
     "Same plate, different car: held as a suspected clone"),
    ("tailgate_in", "Two cars in one pass", "entry", [("JNP3047", "white"), ("RVC5108", "grey")], "day",
     "Tailgating: both vehicles detected and flagged"),
    ("ana_out", "Ana leaves", "exit", [("HLM2056", "yellow")], "day", "Exit"),
]


def _font(size):
    return ImageFont.load_default(size=size)


def render_plate(text: str) -> Image.Image:
    w, h = 520, 260
    img = Image.new("RGB", (w, h), (245, 245, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([4, 4, w - 5, h - 5], outline=(20, 40, 110), width=8)
    d.text((w // 2, 40), "DEMO STATE", font=_font(34), fill=(20, 40, 110), anchor="mm")
    label, size = f"{text[:3]} {text[3:]}", 130
    while size > 40 and d.textlength(label, font=_font(size)) > w - 60:
        size -= 4
    d.text((w // 2, 145), label, font=_font(size), fill=(15, 15, 20), anchor="mm")
    d.text((w // 2, 228), "FICTIONAL", font=_font(24), fill=(120, 20, 20), anchor="mm")
    return img


def draw_car(img: Image.Image, cx: int, base_y: int, scale: float, plate: str, color: str):
    d = ImageDraw.Draw(img)
    body = COLORS[color]
    s = scale
    def R(x1, y1, x2, y2):
        return [cx + x1 * s, base_y + y1 * s, cx + x2 * s, base_y + y2 * s]
    roof = tuple(max(0, c - 40) for c in body)
    d.rounded_rectangle(R(-380, -400, 380, 0), radius=int(60 * s), fill=body)
    d.rounded_rectangle(R(-300, -490, 300, -340), radius=int(40 * s), fill=roof)
    d.rectangle(R(-270, -470, 270, -360), fill=(40, 50, 60))
    for sx in (-330, 250):
        d.rounded_rectangle(R(sx, -300, sx + 80, -240), radius=int(10 * s), fill=(220, 25, 25))
    d.rectangle(R(-400, 0, 400, 40), fill=(25, 25, 25))
    for wx in (-340, 260):
        d.rectangle(R(wx, 10, wx + 80, 80), fill=(15, 15, 15))
    p = render_plate(plate).resize((max(8, int(260 * s)), max(4, int(130 * s))))
    img.paste(p, (int(cx - 130 * s), int(base_y - 160 * s)))


def render_frame(cars: list[tuple[str, str]], t: float, rng: random.Random) -> np.ndarray:
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (150, 175, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 430, W, H], fill=(72, 72, 78))
    d.rectangle([0, 425, W, 432], fill=(220, 200, 60))  # stop line
    d.text((24, 20), "DRIVE-OFF DEMO LOT  ·  synthetic camera", font=_font(22), fill=(255, 255, 255))
    scale = 0.62 + 0.38 * t
    if len(cars) == 1:
        positions = [W // 2 + int((1 - t) * 40)]
    else:
        positions = [W // 2 - 270, W // 2 + 300]
        scale *= 0.72
    for (plate, color), cx in zip(cars, positions):
        draw_car(img, cx, int(470 + 180 * t), scale, plate, color)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def degrade(frame: np.ndarray, cond: str, rng: random.Random) -> np.ndarray:
    a = frame.astype(np.float32)
    if cond.startswith("night"):
        a = a * (0.45 if cond == "night_blur" else 0.3) + np.random.default_rng(rng.randint(0, 999)).normal(0, 10, a.shape)
    a = np.clip(a, 0, 255).astype(np.uint8)
    if cond == "angle":
        h, w = a.shape[:2]
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        dst = np.float32([[w * .18, h * .05], [w, 0], [w, h], [w * .18, h * .95]])
        a = cv2.warpPerspective(a, cv2.getPerspectiveTransform(src, dst), (w, h), borderMode=cv2.BORDER_REPLICATE)
    if cond.endswith("blur"):
        k = 13
        kernel = np.zeros((k, k)); kernel[k // 2, :] = 1.0 / k
        a = cv2.filter2D(a, -1, kernel)
    return a


def build_clips(force=False, n_frames=6):
    for cid, title, lane, cars, cond, story in CLIPS:
        d = config.CLIPS_DIR / cid
        if d.exists() and not force and (d / "manifest.json").exists():
            continue
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        rng = random.Random(cid)
        for i in range(n_frames):
            t = i / (n_frames - 1)
            f = degrade(render_frame(cars, t, rng), cond, rng)
            cv2.imwrite(str(d / f"{i:02d}.jpg"), f, [cv2.IMWRITE_JPEG_QUALITY, 85])
        (d / "manifest.json").write_text(json.dumps({
            "id": cid, "title": title, "lane": lane, "condition": cond, "story": story,
            "frames": n_frames, "truth": [p for p, _ in cars]}))


def clip_list() -> list[dict]:
    out = []
    for cid, *_ in CLIPS:
        m = config.CLIPS_DIR / cid / "manifest.json"
        if m.exists():
            out.append(json.loads(m.read_text()))
    return out


def clip_frames(cid: str) -> list[np.ndarray]:
    d = config.CLIPS_DIR / cid
    return [cv2.imread(str(p)) for p in sorted(d.glob("*.jpg"))]


def seed(vault_id: str | None = None):
    """Create the demo lot and fictional drivers (idempotent)."""
    if not db.one("SELECT id FROM lots WHERE id='lot_main'"):
        db.insert("lots", {"id": "lot_main", "name": "Elm Street Community Lot", "address": "Fictional demo lot",
                           "spaces": 40, "timezone": "America/Los_Angeles", "plate_format": "LLLDDDD",
                           "rate_sign": DEMO_SIGN, "rule_json": DEMO_RULE.to_dict(),
                           "rule_confirmed_at": db.iso(db.now()), "demo_offset_min": 0})
        db.insert("lots", {"id": "lot_mall", "name": "Riverside Mall Deck", "address": "Fictional demo lot",
                           "spaces": 120, "timezone": "America/Los_Angeles", "plate_format": "LLLDDDD",
                           "rate_sign": "$2 per hour, max $12 per day", "rule_json": RateRule(
                               rate_per_hour_cents=200, daily_cap_cents=1200, sign_text="$2 per hour, max $12 per day"
                           ).to_dict(), "rule_confirmed_at": db.iso(db.now()), "demo_offset_min": 0})
    for d in DRIVERS:
        if not db.one("SELECT id FROM drivers WHERE plate=?", d["plate"]):
            db.insert("drivers", {"id": db.new_id("drv"), "plate": d["plate"], "display_name": d["name"],
                                  "lot_ids": [], "vault_id": vault_id or "MOCKVAULT-demo",
                                  "payer_email": "sandbox buyer", "status": "active", "consent_at": db.iso(db.now()),
                                  "consent_version": "v1", "created_at": db.iso(db.now()),
                                  "access_token": f"demo-{d['plate'].lower()}"})


def advance_clock(lot_id: str, minutes: int):
    l = db.one("SELECT demo_offset_min FROM lots WHERE id=?", lot_id)
    db.update("lots", lot_id, demo_offset_min=(l["demo_offset_min"] or 0) + minutes)


def demo_now_label(lot_id: str):
    from .engine import lot_now
    return db.iso(lot_now(lot_id))


__all__ = ["build_clips", "clip_list", "clip_frames", "seed", "advance_clock", "timedelta"]
