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

**Result:** _running._

## 4. Demo footage licence

_In progress._ See `ASSETS.md`.
