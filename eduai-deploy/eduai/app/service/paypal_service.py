"""
eduai/app/service/paypal_service.py
PayPal Subscriptions API integration — plan creation, subscription
verification, and webhook signature checking.

Env vars required (.env):
    PAYPAL_CLIENT_ID
    PAYPAL_CLIENT_SECRET
    PAYPAL_MODE            "sandbox" | "live"   (default: sandbox)
    PAYPAL_WEBHOOK_ID      (from PayPal Developer Dashboard, after webhook setup)
"""

import os
import requests

PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
PAYPAL_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"

CLIENT_ID     = os.getenv("PAYPAL_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
WEBHOOK_ID    = os.getenv("PAYPAL_WEBHOOK_ID", "")

_access_token_cache = {"token": None, "expires_at": 0}


def _get_access_token() -> str:
    """OAuth2 client-credentials token, cached until expiry."""
    import time
    if _access_token_cache["token"] and time.time() < _access_token_cache["expires_at"]:
        return _access_token_cache["token"]

    r = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _access_token_cache["token"] = data["access_token"]
    _access_token_cache["expires_at"] = time.time() + data.get("expires_in", 3000) - 60
    return _access_token_cache["token"]


def _headers():
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
    }


def create_product(name: str, description: str) -> str:
    """One-time setup: creates a PayPal 'product' (e.g. 'Learn-Beta Access').
    Returns product_id. Run this once and hardcode the result, or store it."""
    r = requests.post(
        f"{PAYPAL_BASE}/v1/catalogs/products",
        headers=_headers(),
        json={"name": name, "description": description, "type": "SERVICE", "category": "EDUCATIONAL_AND_TEXTBOOKS"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["id"]


def create_plan(product_id: str, plan_name: str, price_usd: str) -> str:
    """One-time setup: creates a monthly billing plan. Returns plan_id.
    Run once per tier (student $3, teacher $6) and store the resulting IDs
    in your .env as PAYPAL_STUDENT_PLAN_ID / PAYPAL_TEACHER_PLAN_ID."""
    payload = {
        "product_id": product_id,
        "name": plan_name,
        "billing_cycles": [{
            "frequency": {"interval_unit": "MONTH", "interval_count": 1},
            "tenure_type": "REGULAR",
            "sequence": 1,
            "total_cycles": 0,  # 0 = infinite, renews until cancelled
            "pricing_scheme": {"fixed_price": {"value": price_usd, "currency_code": "USD"}},
        }],
        "payment_preferences": {
            "auto_bill_outstanding": True,
            "setup_fee_failure_action": "CONTINUE",
            "payment_failure_threshold": 3,
        },
    }
    r = requests.post(f"{PAYPAL_BASE}/v1/billing/plans", headers=_headers(), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def get_subscription(subscription_id: str) -> dict:
    """Fetch live subscription status directly from PayPal — use this to
    verify a subscription_id the frontend sends you before trusting it."""
    r = requests.get(
        f"{PAYPAL_BASE}/v1/billing/subscriptions/{subscription_id}",
        headers=_headers(), timeout=15,
    )
    r.raise_for_status()
    return r.json()


def verify_webhook_signature(headers: dict, body: bytes) -> bool:
    """Verify a webhook actually came from PayPal, not a forged request.
    PayPal requires the raw body + specific headers for this."""
    payload = {
        "auth_algo":         headers.get("Paypal-Auth-Algo"),
        "cert_url":          headers.get("Paypal-Cert-Url"),
        "transmission_id":   headers.get("Paypal-Transmission-Id"),
        "transmission_sig":  headers.get("Paypal-Transmission-Sig"),
        "transmission_time": headers.get("Paypal-Transmission-Time"),
        "webhook_id":        WEBHOOK_ID,
        "webhook_event":     __import__("json").loads(body),
    }
    r = requests.post(
        f"{PAYPAL_BASE}/v1/notifications/verify-webhook-signature",
        headers=_headers(), json=payload, timeout=15,
    )
    if r.status_code != 200:
        return False
    return r.json().get("verification_status") == "SUCCESS"