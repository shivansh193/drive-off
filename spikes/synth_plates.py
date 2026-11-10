"""Render fictional cars-with-plates for the plate-reader spike and demo.

Every plate is random and fictional (state name "DEMO STATE"), so no real person's plate is used.
Each image comes with ground truth and a condition tag: day, night, angle, blur, dirty, confusable.

  python spikes/synth_plates.py --n 60 --out spikes/out/synth
"""
import argparse
import json
import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"
DIGITS = "0123456789"
CONFUSABLE = "08B1I5S2Z"
CONDITIONS = ["day", "night", "angle", "blur", "dirty", "confusable"]


def font(size):
    return ImageFont.load_default(size=size)


def plate_text(rng, confusable=False):
    if confusable:
        pool_l = "BISZO"
        pool_d = "08152"
        return "".join(rng.choice(pool_l) for _ in range(3)) + "".join(rng.choice(pool_d) for _ in range(4))
    return "".join(rng.choice(LETTERS) for _ in range(3)) + "".join(rng.choice(DIGITS) for _ in range(4))


def render_plate(text):
    w, h = 520, 260
    img = Image.new("RGB", (w, h), (245, 245, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([4, 4, w - 5, h - 5], outline=(20, 40, 110), width=8)
    d.text((w // 2, 40), "DEMO STATE", font=font(34), fill=(20, 40, 110), anchor="mm")
    label, size = f"{text[:3]} {text[3:]}", 130
    while size > 40 and d.textlength(label, font=font(size)) > w - 60:  # keep characters inside the plate
        size -= 4
    d.text((w // 2, 145), label, font=font(size), fill=(15, 15, 20), anchor="mm")
    d.text((w // 2, 228), "FICTIONAL", font=font(24), fill=(120, 20, 20), anchor="mm")
    return img


def render_car(rng, plate):
    W, H = 1280, 720
    sky = tuple(rng.randint(120, 200) for _ in range(3))
    img = Image.new("RGB", (W, H), sky)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 470, W, H], fill=(70, 70, 75))  # asphalt
    body = tuple(rng.randint(20, 220) for _ in range(3))
    cx = W // 2 + rng.randint(-120, 120)
    d.rounded_rectangle([cx - 380, 200, cx + 380, 600], radius=60, fill=body)  # rear of car
    d.rounded_rectangle([cx - 300, 110, cx + 300, 260], radius=40, fill=tuple(max(0, c - 40) for c in body))
    d.rectangle([cx - 270, 130, cx + 270, 240], fill=(40, 50, 60))  # rear window
    for sx in (-330, 250):
        d.rounded_rectangle([cx + sx, 300, cx + sx + 80, 360], radius=10, fill=(200, 20, 20))  # tail lights
    d.rectangle([cx - 400, 600, cx + 400, 640], fill=(25, 25, 25))  # bumper
    for wx in (-340, 260):
        d.rectangle([cx + wx, 610, cx + wx + 80, 680], fill=(15, 15, 15))
    p = plate.resize((260, 130))
    img.paste(p, (cx - 130, 440))
    return img, (cx - 130, 440, cx + 130, 570)


def degrade(img, cond, rng):
    a = np.array(img).astype(np.float32)
    if cond == "night":
        a = a * 0.28 + rng.gauss(0, 9)
        a += np.random.default_rng(rng.randint(0, 9999)).normal(0, 10, a.shape)
    elif cond == "dirty":
        dirt = np.random.default_rng(rng.randint(0, 9999)).normal(0, 1, a.shape[:2])
        dirt = cv2.GaussianBlur(dirt, (0, 0), 25)
        dirt = (dirt - dirt.min()) / (np.ptp(dirt) + 1e-6)
        a = a * (1 - 0.45 * dirt[..., None]) + np.array([60, 45, 25]) * 0.45 * dirt[..., None]
    a = np.clip(a, 0, 255).astype(np.uint8)
    if cond == "angle":
        h, w = a.shape[:2]
        k = rng.uniform(0.12, 0.22)
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        dst = np.float32([[w * k, h * 0.05], [w, 0], [w, h], [w * k, h * 0.95]])
        a = cv2.warpPerspective(a, cv2.getPerspectiveTransform(src, dst), (w, h), borderMode=cv2.BORDER_REPLICATE)
    if cond == "blur":
        ksize = rng.choice([9, 13, 17])
        kernel = np.zeros((ksize, ksize)); kernel[ksize // 2, :] = 1.0 / ksize  # horizontal motion blur
        a = cv2.filter2D(a, -1, kernel)
    return Image.fromarray(a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--out", default="spikes/out/synth")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    truth = []
    for i in range(args.n):
        cond = CONDITIONS[i % len(CONDITIONS)]
        text = plate_text(rng, confusable=(cond == "confusable"))
        car, box = render_car(rng, render_plate(text))
        img = degrade(car, cond, rng)
        name = f"{i:03d}_{cond}.jpg"
        img.save(out / name, quality=88)
        truth.append({"file": name, "plate": text, "condition": cond, "box": box})
    (out / "truth.json").write_text(json.dumps(truth, indent=2))
    print(f"wrote {len(truth)} images to {out}")


if __name__ == "__main__":
    main()
