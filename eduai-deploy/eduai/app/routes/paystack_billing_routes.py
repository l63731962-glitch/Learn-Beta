"""
eduai/app/routes/paystack_billing_routes.py
Mirrors billing_routes.py (PayPal) but for Paystack. Kept as a SEPARATE
blueprint/file rather than branching inside billing_routes.py, so the
existing PayPal path is untouched.
"""
import os
from datetime import datetime
from flask import Blueprint, request, jsonify

from eduai.app.database import get_db_direct
from eduai.app import models
from eduai.app.routes.eduai_routes import require_auth
from eduai.app.service import paystack_service

paystack_billing_bp = Blueprint("eduai_paystack_billing", __name__, url_prefix="/eduai/billing/paystack")

STUDENT_PLAN_CODE = os.getenv("PAYSTACK_STUDENT_PLAN_CODE", "")
TEACHER_PLAN_CODE = os.getenv("PAYSTACK_TEACHER_PLAN_CODE", "")

STUDENT_PRICE_NGN = 4500
TEACHER_PRICE_NGN = 9000

CALLBACK_URL = os.getenv(
    "PAYSTACK_CALLBACK_URL",
    "https://learn-beta-10.fly.dev/eduai/billing/paystack/callback"
)


def _plan_for_role(role: str) -> str:
    return TEACHER_PLAN_CODE if role == "teacher" else STUDENT_PLAN_CODE


def _price_for_role(role: str) -> int:
    return TEACHER_PRICE_NGN if role == "teacher" else STUDENT_PRICE_NGN


@paystack_billing_bp.route("/plan-info", methods=["GET"])
@require_auth
def plan_info(user):
    """Frontend calls this to know which Paystack plan_code + amount to use."""
    return jsonify({
        "plan_code": _plan_for_role(user.role.value),
        "amount_ngn": _price_for_role(user.role.value),
        "public_key": paystack_service.PUBLIC_KEY,
        "subscription_status": user.subscription_status,
        "trial_ends_at": user.trial_ends_at.isoformat() if user.trial_ends_at else None,
    }), 200


@paystack_billing_bp.route("/initialize", methods=["POST"])
@require_auth
def initialize(user):
    """Starts a Paystack transaction. Returns an authorization_url the
    frontend redirects the user to (Paystack Inline JS can also be used
    instead of this redirect flow — see learn.html integration notes)."""
    plan_code = _plan_for_role(user.role.value)
    amount_kobo = _price_for_role(user.role.value) * 100

    try:
        result = paystack_service.initialize_transaction(
            email=user.email,
            amount_kobo=amount_kobo,
            plan_code=plan_code,
            callback_url=CALLBACK_URL,
        )
    except Exception as e:
        return jsonify({"error": f"Could not initialize transaction: {e}"}), 502

    return jsonify(result.get("data", {})), 200


@paystack_billing_bp.route("/activate", methods=["POST"])
@require_auth
def activate(user):
    """
    Called by frontend AFTER Paystack's checkout redirects back with a
    `reference`. We verify directly with Paystack — never trust the
    frontend value blindly.
    Body: { reference: str }
    """
    data = request.get_json(force=True) or {}
    reference = (data.get("reference") or "").strip()
    if not reference:
        return jsonify({"error": "reference is required"}), 400

    try:
        result = paystack_service.verify_transaction(reference)
    except Exception as e:
        return jsonify({"error": f"Could not verify transaction: {e}"}), 502

    tx_data = result.get("data", {})
    if tx_data.get("status") != "success":
        return jsonify({"error": f"Transaction not successful (status: {tx_data.get('status')})"}), 402

    subscription_code = tx_data.get("plan_object", {}).get("plan_code") or tx_data.get("plan")
    customer_code = tx_data.get("customer", {}).get("customer_code", "")

    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(id=user.id).first()
        db_user.subscription_status         = "active"
        db_user.payment_provider             = "paystack"
        db_user.paystack_customer_code       = customer_code
        db_user.paystack_plan_code           = subscription_code
        db_user.subscription_updated_at      = datetime.utcnow()
        db.commit()
        return jsonify({"status": "active", "reference": reference}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@paystack_billing_bp.route("/cancel", methods=["POST"])
@require_auth
def cancel(user):
    """User-initiated cancel."""
    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(id=user.id).first()

        if db_user.paystack_subscription_code and db_user.paystack_email_token:
            try:
                paystack_service.disable_subscription(
                    db_user.paystack_subscription_code,
                    db_user.paystack_email_token,
                )
            except Exception as e:
                print(f"[PAYSTACK] Cancel API call failed, flipping local status anyway: {e}")

        db_user.subscription_status     = "cancelled"
        db_user.subscription_updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"status": "cancelled"}), 200
    finally:
        db.close()


@paystack_billing_bp.route("/webhook", methods=["POST"])
def webhook():
    """
    Paystack → your server. Keeps subscription_status correct even if the
    user never reopens the app (renewals, failures, cancellations).
    """
    raw_body = request.get_data()
    if not paystack_service.verify_webhook_signature(dict(request.headers), raw_body):
        return jsonify({"error": "invalid signature"}), 400

    event = request.get_json(force=True) or {}
    event_type = event.get("event", "")
    data = event.get("data", {})

    customer_code = data.get("customer", {}).get("customer_code", "")
    subscription_code = data.get("subscription_code", "") or data.get("plan", {}).get("plan_code", "")
    email_token = data.get("email_token", "")

    if not customer_code:
        return jsonify({"status": "ignored"}), 200

    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(
            paystack_customer_code=customer_code
        ).first()
        if not db_user:
            return jsonify({"status": "no matching user"}), 200

        if event_type == "subscription.create":
            db_user.subscription_status = "active"
            if subscription_code:
                db_user.paystack_subscription_code = subscription_code
            if email_token:
                db_user.paystack_email_token = email_token
        elif event_type == "charge.success":
            db_user.subscription_status = "active"
        elif event_type == "subscription.disable":
            db_user.subscription_status = "cancelled"
        elif event_type == "invoice.payment_failed":
            db_user.subscription_status = "past_due"
        elif event_type == "subscription.not_renew":
            db_user.subscription_status = "expired"

        db_user.subscription_updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"status": "processed"}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


def register_paystack_billing_routes(app):
    app.register_blueprint(paystack_billing_bp)
    print("[EDUAI-PAYSTACK] ✅ Paystack billing routes registered at /eduai/billing/paystack")
