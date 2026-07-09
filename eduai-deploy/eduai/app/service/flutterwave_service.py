"""
eduai/app/service/flutterwave_service.py
Flutterwave Payments API integration — transaction initialization,
verification, subscription management, and webhook signature checking.

Env vars required (.env):
    FLUTTERWAVE_SECRET_KEY
    FLUTTERWAVE_PUBLIC_KEY
    FLUTTERWAVE_SECRET_HASH        (set this same string in the Flutterwave
                                     dashboard under Webhooks → Secret Hash)
    FLUTTERWAVE_STUDENT_PLAN_ID    (from Flutterwave Dashboard → Payment Plans)
    FLUTTERWAVE_TEACHER_PLAN_ID
"""

import os
import uuid
import requests

SECRET_KEY = os.getenv("FLUTTERWAVE_SECRET_KEY", "")
PUBLIC_KEY = os.getenv("FLUTTERWAVE_PUBLIC_KEY", "")
SECRET_HASH = os.getenv("FLUTTERWAVE_SECRET_HASH", "")
BASE_URL = "https://api.flutterwave.com/v3"


def _headers():
    return {
        "Authorization": f"Bearer {SECRET_KEY}",
        "Content-Type": "application/json",
    }


def generate_tx_ref(prefix: str = "eduai") -> str:
    """Flutterwave requires YOU to generate a unique tx_ref per transaction
    (Flutterwave will not generate one for you)."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def initialize_transaction(email: str, amount_ngn: int, plan_id: str,
                            callback_url: str, tx_ref: str, name: str = "") -> dict:
    """
    Starts a Flutterwave standard checkout transaction. Returns a payload
    containing a `link` the frontend should redirect the user to.
    Passing `payment_plan` attaches the transaction to a recurring plan —
    Flutterwave then auto-creates a subscription on first successful charge.
    """
    payload = {
        "tx_ref": tx_ref,
        "amount": amount_ngn,
        "currency": "NGN",
        "redirect_url": callback_url,
        "payment_plan": plan_id,
        "customer": {
            "email": email,
            "name": name or email,
        },
        "customizations": {
            "title": "EduAI Subscription",
        },
    }
    r = requests.post(
        f"{BASE_URL}/payments",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def verify_transaction(transaction_id: str) -> dict:
    """
    Confirms a transaction actually succeeded — never trust the frontend's
    redirect params alone. Flutterwave verifies by transaction_id (the
    numeric `id` field), NOT by the tx_ref you originally sent.
    """
    r = requests.get(
        f"{BASE_URL}/transactions/{transaction_id}/verify",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_subscription(subscription_id: str) -> dict:
    """Fetch a single subscription's live status directly from Flutterwave."""
    r = requests.get(
        f"{BASE_URL}/subscriptions/{subscription_id}",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def cancel_subscription(subscription_id: str) -> dict:
    """Cancels a subscription by its Flutterwave-assigned numeric ID."""
    r = requests.put(
        f"{BASE_URL}/subscriptions/{subscription_id}/cancel",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def verify_webhook_signature(headers: dict, body: bytes) -> bool:
    """
    Flutterwave does NOT use HMAC signing. It sends back the exact
    secret hash string you configured in the dashboard, in the
    `verif-hash` header. Compare it directly (constant-time) against your
    configured FLUTTERWAVE_SECRET_HASH.
    """
    import hmac
    received = headers.get("verif-hash") or headers.get("Verif-Hash")
    if not received or not SECRET_HASH:
        return False
    return hmac.compare_digest(received, SECRET_HASH)