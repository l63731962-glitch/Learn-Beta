"""
eduai/app/service/paystack_service.py
Paystack Subscriptions API integration — plan verification, transaction
initialization, and webhook signature checking.

Env vars required (.env):
    PAYSTACK_SECRET_KEY
    PAYSTACK_PUBLIC_KEY
    PAYSTACK_STUDENT_PLAN_CODE   (from Paystack Dashboard → Plans)
    PAYSTACK_TEACHER_PLAN_CODE
"""

import os
import hmac
import hashlib
import requests

SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "")
BASE_URL = "https://api.paystack.co"


def _headers():
    return {
        "Authorization": f"Bearer {SECRET_KEY}",
        "Content-Type": "application/json",
    }


def initialize_transaction(email: str, amount_kobo: int, plan_code: str, callback_url: str) -> dict:
    """
    Starts a transaction that will attach the customer to a subscription
    plan once they complete payment. amount_kobo must match (or exceed)
    the plan's amount — Paystack uses the plan's amount for recurring
    billing regardless, but the first charge uses what you pass here.
    """
    payload = {
        "email": email,
        "amount": amount_kobo,
        "plan": plan_code,
        "callback_url": callback_url,
    }
    r = requests.post(
        f"{BASE_URL}/transaction/initialize",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def verify_transaction(reference: str) -> dict:
    """Confirms a transaction actually succeeded — never trust the
    frontend's callback alone."""
    r = requests.get(
        f"{BASE_URL}/transaction/verify/{reference}",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_subscription(subscription_code: str) -> dict:
    """Fetch live subscription status directly from Paystack."""
    r = requests.get(
        f"{BASE_URL}/subscription/{subscription_code}",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def disable_subscription(subscription_code: str, email_token: str) -> dict:
    """Cancels a subscription. email_token comes from the subscription
    fetch/webhook payload (Paystack requires it alongside the code)."""
    payload = {"code": subscription_code, "token": email_token}
    r = requests.post(
        f"{BASE_URL}/subscription/disable",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def verify_webhook_signature(headers: dict, body: bytes) -> bool:
    """Paystack signs webhooks with HMAC-SHA512 using your secret key.
    Compare against the x-paystack-signature header."""
    signature = headers.get("x-paystack-signature") or headers.get("X-Paystack-Signature")
    if not signature:
        return False
    computed = hmac.new(
        SECRET_KEY.encode("utf-8"),
        body,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)
