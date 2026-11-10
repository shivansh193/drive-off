"""PayPal sandbox client: vault-with-authorization enrolment, merchant-initiated charge, refund, webhook verify.

Every money-moving call carries a deterministic PayPal-Request-Id, so retries and duplicate camera reads
replay the original result instead of charging twice. PAYPAL_MODE=mock gives an in-memory fake with the same
idempotency semantics (used by tests and by anyone running the repo without sandbox keys).
"""
from __future__ import annotations

import threading
import time
import uuid

import requests

from . import config

_token = {"value": None, "exp": 0.0}
_tlock = threading.Lock()


class PayPalError(Exception):
    def __init__(self, status, body):
        super().__init__(f"PayPal {status}: {body.get('name') or body}")
        self.status, self.body = status, body


def _access_token() -> str:
    with _tlock:
        if _token["value"] and time.time() < _token["exp"] - 60:
            return _token["value"]
        r = requests.post(f"{config.PAYPAL_API_BASE}/v1/oauth2/token",
                          auth=(config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET),
                          data={"grant_type": "client_credentials"}, timeout=30)
        r.raise_for_status()
        d = r.json()
        _token.update(value=d["access_token"], exp=time.time() + d.get("expires_in", 3000))
        return _token["value"]


def _call(method, path, body=None, request_id=None, retries=2):
    for attempt in range(retries + 1):
        h = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}
        if request_id:
            h["PayPal-Request-Id"] = request_id
        try:
            r = requests.request(method, f"{config.PAYPAL_API_BASE}{path}", headers=h, json=body, timeout=60)
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
            continue  # safe: same request id
        if r.status_code >= 500 and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise PayPalError(r.status_code, data)
        return data


# ---------------------------------------------------------------- mock
class _Mock:
    def __init__(self):
        self.by_request: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def idem(self, rid, fn):
        if rid in self.by_request:
            return self.by_request[rid]
        res = fn()
        self.by_request[rid] = res
        return res


MOCK = _Mock()


def mode() -> str:
    return config.PAYPAL_MODE


# ---------------------------------------------------------------- enrolment
def create_enrol_order(driver_id: str, return_url: str, cancel_url: str) -> dict:
    """$1.00 AUTHORIZE that vaults the PayPal wallet for merchant-initiated use. Voided right after approval."""
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{"reference_id": driver_id, "custom_id": driver_id,
                            "description": "Drive-Off account check: $1.00 hold, released immediately",
                            "amount": {"currency_code": config.CURRENCY, "value": "1.00"}}],
        "payment_source": {"paypal": {
            "attributes": {"vault": {"store_in_vault": "ON_SUCCESS", "usage_type": "MERCHANT",
                                     "usage_pattern": "IMMEDIATE", "customer_type": "CONSUMER"}},
            "experience_context": {"shipping_preference": "NO_SHIPPING", "brand_name": "Drive-Off (sandbox)",
                                   "user_action": "CONTINUE", "return_url": return_url, "cancel_url": cancel_url},
        }},
    }
    if mode() == "mock":
        oid = f"MOCKORDER{uuid.uuid4().hex[:8].upper()}"
        MOCK.orders[oid] = {"id": oid, "driver": driver_id, "status": "PAYER_ACTION_REQUIRED"}
        return {"id": oid, "approve_url": f"{return_url}{'&' if '?' in return_url else '?'}token={oid}&mock=1"}
    d = _call("POST", "/v2/checkout/orders", body, request_id=f"enrol-{driver_id}-{uuid.uuid4().hex[:6]}")
    return {"id": d["id"], "approve_url": next(l["href"] for l in d["links"] if l["rel"] == "payer-action")}


def finish_enrolment(order_id: str) -> dict:
    """Authorize the approved order, read the vault id, void the $1 hold."""
    if mode() == "mock":
        return {"vault_id": f"MOCKVAULT{order_id[-6:]}", "customer_id": "MOCKCUST", "payer_email": "buyer@example.com",
                "vault_status": "VAULTED", "authorization_id": "MOCKAUTH", "voided": True}
    d = _call("POST", f"/v2/checkout/orders/{order_id}/authorize", {}, request_id=f"auth-{order_id}")
    pp = d.get("payment_source", {}).get("paypal", {})
    v = pp.get("attributes", {}).get("vault", {})
    auth = d["purchase_units"][0]["payments"]["authorizations"][0]
    voided = False
    try:
        _call("POST", f"/v2/payments/authorizations/{auth['id']}/void", None, request_id=f"void-{auth['id']}")
        voided = True
    except PayPalError:
        pass
    return {"vault_id": v.get("id"), "customer_id": v.get("customer", {}).get("id"),
            "payer_email": pp.get("email_address"), "vault_status": v.get("status"),
            "authorization_id": auth["id"], "voided": voided}


# ---------------------------------------------------------------- charge / refund
def charge(session_id: str, vault_id: str, amount_cents: int, description: str) -> dict:
    """Merchant-initiated charge on the vaulted wallet. Idempotent per session: `exit-<session_id>`."""
    rid = f"exit-{session_id}"
    value = f"{amount_cents / 100:.2f}"
    if mode() == "mock":
        MOCK.calls.append(("charge", rid, value))

        def make():
            oid, cid = f"MOCKO{uuid.uuid4().hex[:10].upper()}", f"MOCKC{uuid.uuid4().hex[:10].upper()}"
            MOCK.captures[cid] = {"amount": value, "refunded": "0.00"}
            return {"order_id": oid, "capture_id": cid, "status": "COMPLETED", "amount": value, "request_id": rid}
        return MOCK.idem(rid, make)
    d = _call("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{"reference_id": session_id, "custom_id": session_id, "invoice_id": session_id,
                            "description": description[:127],
                            "amount": {"currency_code": config.CURRENCY, "value": value}}],
        "payment_source": {"paypal": {"vault_id": vault_id}},
    }, request_id=rid)
    if d.get("status") == "APPROVED":
        d = _call("POST", f"/v2/checkout/orders/{d['id']}/capture", {}, request_id=f"cap-{rid}")
    cap = d["purchase_units"][0]["payments"]["captures"][0]
    return {"order_id": d["id"], "capture_id": cap["id"], "status": cap["status"],
            "amount": cap["amount"]["value"], "request_id": rid}


def refund(capture_id: str, amount_cents: int | None, request_id: str, note: str) -> dict:
    body = {"note_to_payer": note[:255]}
    if amount_cents is not None:
        body["amount"] = {"currency_code": config.CURRENCY, "value": f"{amount_cents / 100:.2f}"}
    if mode() == "mock":
        return MOCK.idem(request_id, lambda: {"refund_id": f"MOCKR{uuid.uuid4().hex[:10].upper()}",
                                              "status": "COMPLETED"})
    d = _call("POST", f"/v2/payments/captures/{capture_id}/refund", body, request_id=request_id)
    return {"refund_id": d["id"], "status": d["status"]}


# ---------------------------------------------------------------- webhooks
def verify_webhook(headers: dict, event: dict) -> tuple[bool, str]:
    if mode() == "mock":
        return False, "mock mode: not verified"
    if not config.PAYPAL_WEBHOOK_ID:
        return False, "PAYPAL_WEBHOOK_ID not set"
    h = {k.lower(): v for k, v in headers.items()}
    try:
        d = _call("POST", "/v1/notifications/verify-webhook-signature", {
            "auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"), "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"), "webhook_id": config.PAYPAL_WEBHOOK_ID,
            "webhook_event": event,
        })
    except PayPalError as e:
        return False, str(e)
    return d.get("verification_status") == "SUCCESS", d.get("verification_status", "?")


def ensure_webhook(url: str, events: list[str]) -> dict:
    """Create (or find) this app's webhook for `url`."""
    existing = _call("GET", "/v1/notifications/webhooks").get("webhooks", [])
    for w in existing:
        if w["url"] == url:
            return w
    return _call("POST", "/v1/notifications/webhooks",
                 {"url": url, "event_types": [{"name": e} for e in events]})
