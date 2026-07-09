"""
eduai/app/routes/flutterwave_billing_routes.py
Handles Flutterwave subscription checkout, activation, cancellation, and
webhook events. Kept as a SEPARATE blueprint/file from billing_routes.py
(PayPal), so the existing PayPal path is untouched.
"""
import os
from datetime import datetime
from flask import Blueprint, request, jsonify

from eduai.app.database import get_db_direct
from eduai.app import models
from eduai.app.routes.eduai_routes import require_auth
from eduai.app.service import flutterwave_service

flutterwave_billing_bp = Blueprint(
    "eduai_flutterwave_billing", __name__, url_prefix="/eduai/billing/flutterwave"
)

STUDENT_PLAN_ID = os.getenv("FLUTTERWAVE_STUDENT_PLAN_ID", "")
TEACHER_PLAN_ID = os.getenv("FLUTTERWAVE_TEACHER_PLAN_ID", "")

STUDENT_PRICE_NGN = 4500
TEACHER_PRICE_NGN = 9000

CALLBACK_URL = os.getenv(
    "FLUTTERWAVE_CALLBACK_URL",
    "https://learn-beta-10.fly.dev/eduai/billing/flutterwave/callback"
)


def _plan_for_role(role: str) -> str:
    return TEACHER_PLAN_ID if role == "teacher" else STUDENT_PLAN_ID


def _price_for_role(role: str) -> int:
    return TEACHER_PRICE_NGN if role == "teacher" else STUDENT_PRICE_NGN


@flutterwave_billing_bp.route("/plan-info", methods=["GET"])
@require_auth
def plan_info(user):
    """Frontend calls this to know which Flutterwave plan_id + amount to use."""
    return jsonify({
        "plan_id": _plan_for_role(user.role.value),
        "amount_ngn": _price_for_role(user.role.value),
        "public_key": flutterwave_service.PUBLIC_KEY,
        "subscription_status": user.subscription_status,
        "trial_ends_at": user.trial_ends_at.isoformat() if user.trial_ends_at else None,
    }), 200


@flutterwave_billing_bp.route("/initialize", methods=["POST"])
@require_auth
def initialize(user):
    """Starts a Flutterwave transaction. Returns { link, tx_ref } — the
    frontend redirects the user to `link` to complete checkout."""
    plan_id = _plan_for_role(user.role.value)
    amount = _price_for_role(user.role.value)
    tx_ref = flutterwave_service.generate_tx_ref()

    try:
        result = flutterwave_service.initialize_transaction(
            email=user.email,
            amount_ngn=amount,
            plan_id=plan_id,
            callback_url=CALLBACK_URL,
            tx_ref=tx_ref,
            name=user.name,
        )
    except Exception as e:
        return jsonify({"error": f"Could not initialize transaction: {e}"}), 502

    data = result.get("data", {})
    return jsonify({
        "link": data.get("link", ""),
        "tx_ref": tx_ref,
    }), 200


@flutterwave_billing_bp.route("/activate", methods=["POST"])
@require_auth
def activate(user):
    """
    Called by frontend AFTER Flutterwave's redirect returns with a
    transaction_id. We verify directly with Flutterwave — never trust the
    frontend value blindly.
    Body: { transaction_id: str }
    """
    data = request.get_json(force=True) or {}
    transaction_id = (data.get("transaction_id") or "").strip()
    if not transaction_id:
        return jsonify({"error": "transaction_id is required"}), 400

    try:
        result = flutterwave_service.verify_transaction(transaction_id)
    except Exception as e:
        return jsonify({"error": f"Could not verify transaction: {e}"}), 502

    tx_data = result.get("data", {})
    if tx_data.get("status") != "successful":
        return jsonify({"error": f"Transaction not successful (status: {tx_data.get('status')})"}), 402

    customer_id = str(tx_data.get("customer", {}).get("id", ""))
    # Flutterwave returns the plan's subscription id on the verify response
    # when the transaction was tied to a payment_plan. Save it here so
    # cancel() can call Flutterwave's cancel API immediately, without
    # waiting on a webhook to backfill it later.
    subscription_id = str(tx_data.get("plan", "") or "")

    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(id=user.id).first()
        db_user.subscription_status       = "active"
        db_user.payment_provider           = "flutterwave"
        db_user.flutterwave_customer_id    = customer_id
        if subscription_id:
            db_user.flutterwave_subscription_id = subscription_id
        db_user.subscription_updated_at    = datetime.utcnow()
        db.commit()
        return jsonify({"status": "active", "transaction_id": transaction_id}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@flutterwave_billing_bp.route("/cancel", methods=["POST"])
@require_auth
def cancel(user):
    """User-initiated cancel."""
    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(id=user.id).first()

        if db_user.flutterwave_subscription_id:
            try:
                flutterwave_service.cancel_subscription(db_user.flutterwave_subscription_id)
            except Exception as e:
                print(f"[FLUTTERWAVE] Cancel API call failed, flipping local status anyway: {e}")

        db_user.subscription_status     = "cancelled"
        db_user.subscription_updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"status": "cancelled"}), 200
    finally:
        db.close()


@flutterwave_billing_bp.route("/webhook", methods=["POST"])
def webhook():
    """
    Flutterwave → your server. Keeps subscription_status correct even if
    the user never reopens the app (renewals, failures, cancellations).
    """
    if not flutterwave_service.verify_webhook_signature(dict(request.headers), request.get_data()):
        return jsonify({"error": "invalid signature"}), 400

    event = request.get_json(force=True) or {}
    event_type = event.get("event", "")
    data = event.get("data", {})

    customer_id = str(data.get("customer", {}).get("id", ""))
    subscription_id = str(data.get("id", "")) if event_type.startswith("subscription") else ""

    if not customer_id:
        return jsonify({"status": "ignored"}), 200

    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(
            flutterwave_customer_id=customer_id
        ).first()
        if not db_user:
            return jsonify({"status": "no matching user"}), 200

        if event_type == "subscription.activated" or event_type == "charge.completed":
            db_user.subscription_status = "active"
            if subscription_id:
                db_user.flutterwave_subscription_id = subscription_id
        elif event_type == "subscription.cancelled":
            db_user.subscription_status = "cancelled"
        elif event_type == "charge.failed":
            db_user.subscription_status = "past_due"

        db_user.subscription_updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"status": "processed"}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


def register_flutterwave_billing_routes(app):
    app.register_blueprint(flutterwave_billing_bp)
    print("[EDUAI-FLUTTERWAVE] ✅ Flutterwave billing routes registered at /eduai/billing/flutterwave")