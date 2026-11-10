# Spikes: verify before building any UI

Status as of 2026-11-10. Each spike records what was run, what happened, and what it means for the build.

## 1. PayPal Vault → merchant-initiated charge → refund

**Docs checked**
- Vault without purchase ([Payment Method Tokens v3](https://developer.paypal.com/docs/checkout/save-payment-methods/purchase-later/payment-tokens-api/paypal/)):
  `POST /v3/vault/setup-tokens` (`usage_type: MERCHANT`, `usage_pattern: IMMEDIATE`) → buyer approves the `approve` link →
  `POST /v3/vault/payment-tokens` with `{token: {id, type: SETUP_TOKEN}}` → payment token id (`vault_id`).
- Vault with purchase ([Orders v2](https://developer.paypal.com/docs/checkout/save-payment-methods/during-purchase/orders-api/paypal/)):
  `payment_source.paypal.attributes.vault.store_in_vault: ON_SUCCESS`; the vault id comes back on capture.
- Charge later with no buyer: `POST /v2/checkout/orders` with `intent: CAPTURE` and `payment_source.paypal.vault_id`.
  The documented response is already `COMPLETED` with a capture, so no separate capture call is needed. The spike still handles `APPROVED` by capturing.
- Idempotency: the `PayPal-Request-Id` header. Drive-Off uses `exit-<session_id>`, so a re-read at the exit replays the same order instead of creating a new one.
- Refund: `POST /v2/payments/captures/{id}/refund`.

**Known risks from the docs**
- Vault must be ticked under the sandbox app's *Advanced options*.
- The docs say that saving PayPal as a payment source needs reference-transaction / billing-agreement approval on the business account. This is usually enabled on US sandbox business accounts, and the spike will confirm it.
- Risk Data Acquisition (RDA) is required for *customer-initiated* token payments. Drive-Off's exit charge is merchant-initiated.

**Script:** `spikes/paypal_spike.py` (`setup` → approve in browser → `vault` → `charge` → `charge-again` → `refund`).

**Result:** _pending sandbox credentials._

## 2. Webhooks arrive and verify

Plan: register a sandbox webhook for `VAULT.PAYMENT-TOKEN.CREATED`, `PAYMENT.CAPTURE.COMPLETED` and `PAYMENT.CAPTURE.REFUNDED` against the Render URL, then verify each one with `POST /v1/notifications/verify-webhook-signature`.

**Result:** _pending credentials and a public URL._

## 3. Plate reader accuracy and calibration

Stack: [fast-alpr](https://github.com/ankandrew/fast-alpr) (MIT), which combines a YOLOv9-tiny plate detector with fast-plate-ocr (ONNX, CPU). The second pass sends low-confidence crops to Gemini.
Eval set: synthetic fictional plates (`spikes/synth_plates.py`) in six conditions (day, night, angle, blur, dirty, confusable characters).

**Result (single frame, no voting, no LLM pass, 60 synthetic images, CPU):**

| condition | n | exact | per-char |
|---|---|---|---|
| day | 10 | 100% | 100% |
| dirty | 10 | 100% | 100% |
| night | 10 | 90% | 98.6% |
| angle | 10 | 80% | 88.6% |
| blur | 10 | 70% | 77.1% |
| confusable (0/O, 1/I, 8/B…) | 10 | 70% | 87.1% |
| **all** | 60 | **85%** | **91.9%** |

- Detection: 57/60. Latency: about 100 ms/frame on a laptop CPU, so no GPU or Kaggle is needed and live webcam inference is feasible on Render.
- Calibration: OCR confidence ≥ 0.95 → 94% exact (54 images). Below 0.95, 0 of 6 were exact. The raw score is useful, but **it is not safe on its own**: 3 wrong reads still scored ≥ 0.95, nearly all of them I↔1/7 confusions (`ISZ0522`→`1SZ0522`, `TYK9340`→`7YK9340`).
- An early renderer bug (text overflowing the plate) caused 10% exact. It was fixed and re-run. Recorded here so the numbers are honest.

**What this means for the design.** Auto-charge requires all of these:
1. multi-frame voting (≥ 3 agreeing frames per pass);
2. format-aware correction for the lot's plate pattern (letter positions map 1→I, 0→O, 8→B, 5→S, 2→Z);
3. an **exact** match to an enrolled plate at both entry and exit;
4. no other enrolled plate within edit distance 1. Otherwise the read goes to the review queue, and the Gemini second pass looks at the crop.

A misread can then only charge someone if it turns into a *different enrolled* plate, and rule 4 blocks that case explicitly.

## 4. Demo footage licence

- **Primary: synthetic, fictional plates** rendered by `spikes/synth_plates.py`. We create them, so the licence is MIT, every plate is fictional ("DEMO STATE / FICTIONAL"), and there's no privacy risk.
- **Live webcam mode:** a printed fictional plate, or the user's own or a consenting person's car.
- **Optional real-photo eval:** the Open Images "Vehicle registration plate" subset (images CC BY 2.0, boxes CC BY 4.0, attribution recorded in `ASSETS.md`). It's used for a detection-only eval and is **never shown in the video**.
- No scraped footage.
