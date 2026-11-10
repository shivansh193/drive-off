"""Spike 1: vault a sandbox PayPal wallet (no purchase), charge it merchant-initiated, refund it.

Usage (sandbox only; reads PAYPAL_* from .env):
  python spikes/paypal_spike.py setup            # prints an approve link; open it, log in as the sandbox PERSONAL buyer
  python spikes/paypal_spike.py vault            # exchanges the approved setup token for a vault payment token
  python spikes/paypal_spike.py charge 4.50      # merchant-initiated Orders v2 charge, no buyer present
  python spikes/paypal_spike.py charge-again     # repeats the same request id: must NOT double-charge
  python spikes/paypal_spike.py refund           # Payments v2 refund of the last capture

State is kept in spikes/out/state.json (gitignored). Secrets are never printed.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
BASE = os.environ.get("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")
if "sandbox" not in BASE:
    sys.exit("Refusing to run: PAYPAL_API_BASE is not a sandbox URL.")
APP = os.environ.get("APP_BASE_URL", "http://localhost:8000")
STATE = ROOT / "spikes" / "out" / "state.json"


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def token():
    r = requests.post(
        f"{BASE}/v1/oauth2/token",
        auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def call(method, path, body=None, request_id=None):
    h = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    if request_id:
        h["PayPal-Request-Id"] = request_id
    t0 = time.time()
    r = requests.request(method, f"{BASE}{path}", headers=h, json=body, timeout=60)
    dt = int((time.time() - t0) * 1000)
    print(f"{method} {path} -> {r.status_code} ({dt} ms), debug_id={r.headers.get('Paypal-Debug-Id')}")
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text}
    if r.status_code >= 400:
        print(json.dumps(data, indent=2))
        sys.exit(1)
    return data


def setup():
    d = call("POST", "/v3/vault/setup-tokens", {
        "payment_source": {"paypal": {
            "description": "Drive-Off: pay for parking at posted rates when you leave the lot",
            "permit_multiple_payment_tokens": False,
            "usage_pattern": "IMMEDIATE",
            "usage_type": "MERCHANT",
            "customer_type": "CONSUMER",
            "experience_context": {
                "shipping_preference": "NO_SHIPPING",
                "brand_name": "Drive-Off (sandbox)",
                "return_url": f"{APP}/enrol/return",
                "cancel_url": f"{APP}/enrol/cancel",
            },
        }}
    }, request_id=str(uuid.uuid4()))
    s = load(); s["setup_token"] = d["id"]; s["customer_id"] = d.get("customer", {}).get("id"); save(s)
    approve = next(l["href"] for l in d["links"] if l["rel"] == "approve")
    print("status:", d["status"])
    print("Open this link and approve as the sandbox PERSONAL account:\n ", approve)


def vault():
    s = load()
    d = call("POST", "/v3/vault/payment-tokens",
             {"payment_source": {"token": {"id": s["setup_token"], "type": "SETUP_TOKEN"}}},
             request_id=str(uuid.uuid4()))
    s["vault_id"] = d["id"]; s["customer_id"] = d.get("customer", {}).get("id"); save(s)
    print("vault_id:", d["id"], "customer:", s["customer_id"])


def charge(amount, request_id=None):
    s = load()
    rid = request_id or f"exit-{uuid.uuid4()}"
    d = call("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": "spike-session",
            "description": "Parking: spike test session",
            "amount": {"currency_code": "USD", "value": amount},
        }],
        "payment_source": {"paypal": {"vault_id": s["vault_id"]}},
    }, request_id=rid)
    print("order:", d["id"], "status:", d["status"])
    if d["status"] == "APPROVED":  # some accounts need an explicit capture
        d = call("POST", f"/v2/checkout/orders/{d['id']}/capture", {}, request_id=f"cap-{rid}")
    cap = d["purchase_units"][0]["payments"]["captures"][0]
    print("capture:", cap["id"], cap["status"], cap["amount"])
    s.update(order_id=d["id"], capture_id=cap["id"], last_request_id=rid, last_amount=amount); save(s)
    return d


def charge_again():
    s = load()
    first = s["order_id"]
    d = charge(s["last_amount"], request_id=s["last_request_id"])
    print("IDEMPOTENT" if d["id"] == first else "!! DIFFERENT ORDER: double-charge risk")


def refund():
    s = load()
    d = call("POST", f"/v2/payments/captures/{s['capture_id']}/refund",
             {"note_to_payer": "Drive-Off dispute upheld"}, request_id=f"refund-{s['capture_id']}")
    print("refund:", d["id"], d["status"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"setup": setup, "vault": vault, "charge-again": charge_again, "refund": refund}.get(
        cmd, lambda: charge(sys.argv[2] if len(sys.argv) > 2 else "4.50") if cmd == "charge" else print(__doc__)
    )()
