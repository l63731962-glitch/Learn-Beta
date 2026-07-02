"""
eduai/app/routes/billing_routes.py
Subscription lifecycle: get plan info, activate after PayPal approval,
cancel, and handle PayPal webhook events (renewals, failures, cancellations).
"""
import os
from datetime import datetime
from flask import Blueprint, request, jsonify

from eduai.app.database import get_db_direct
from eduai.app import models
from eduai.app.routes.eduai_routes import require_auth
from eduai.app.service import paypal_service

billing_bp = Blueprint("eduai_billing", __name__, url_prefix="/eduai/billing")

STUDENT_PLAN_ID = os.getenv("PAYPAL_STUDENT_PLAN_ID", "")
TEACHER_PLAN_ID = os.getenv("PAYPAL_TEACHER_PLAN_ID", "")


def _plan_for_role(role: str) -> str:
    return TEACHER_PLAN_ID if role == "teacher" else STUDENT_PLAN_ID


@billing_bp.route("/plan-info", methods=["GET"])
@require_auth
def plan_info(user):
    """Frontend calls this to know which PayPal plan_id to render the button for."""
    return jsonify({
        "plan_id": _plan_for_role(user.role.value),
        "price": 6 if user.role.value == "teacher" else 3,
        "subscription_status": user.subscription_status,
        "trial_ends_at": user.trial_ends_at.isoformat() if user.trial_ends_at else None,
    }), 200


@billing_bp.route("/activate", methods=["POST"])
@require_auth
def activate(user):
    """
    Called by frontend AFTER PayPal's approval flow returns a subscriptionID.
    Body: { subscription_id: str }
    We verify directly with PayPal — never trust the frontend value blindly.
    """
    data = request.get_json(force=True) or {}
    subscription_id = (data.get("subscription_id") or "").strip()
    if not subscription_id:
        return jsonify({"error": "subscription_id is required"}), 400

    try:
        sub = paypal_service.get_subscription(subscription_id)
    except Exception as e:
        return jsonify({"error": f"Could not verify subscription: {e}"}), 502

    if sub.get("status") != "ACTIVE":
        return jsonify({"error": f"Subscription not active (status: {sub.get('status')})"}), 402

    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(id=user.id).first()
        db_user.subscription_status     = "active"
        db_user.paypal_subscription_id  = subscription_id
        db_user.paypal_plan_id          = sub.get("plan_id", "")
        db_user.subscription_updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"status": "active", "subscription_id": subscription_id}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@billing_bp.route("/cancel", methods=["POST"])
@require_auth
def cancel(user):
    """User-initiated cancel. Flips local status immediately."""
    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(id=user.id).first()
        db_user.subscription_status     = "cancelled"
        db_user.subscription_updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"status": "cancelled"}), 200
    finally:
        db.close()


@billing_bp.route("/webhook", methods=["POST"])
def webhook():
    """
    PayPal → your server. Keeps subscription_status correct even if the
    user never reopens the app (renewals, failures, cancellations).
    """
    raw_body = request.get_data()
    if not paypal_service.verify_webhook_signature(dict(request.headers), raw_body):
        return jsonify({"error": "invalid signature"}), 400

    event = request.get_json(force=True) or {}
    event_type = event.get("event_type", "")
    resource = event.get("resource", {})
    subscription_id = resource.get("id", "")

    if not subscription_id:
        return jsonify({"status": "ignored"}), 200

    db = get_db_direct()
    try:
        db_user = db.query(models.User).filter_by(
            paypal_subscription_id=subscription_id
        ).first()
        if not db_user:
            return jsonify({"status": "no matching user"}), 200

        if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
            db_user.subscription_status = "active"
        elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
            db_user.subscription_status = "cancelled"
        elif event_type == "BILLING.SUBSCRIPTION.SUSPENDED":
            db_user.subscription_status = "past_due"
        elif event_type == "PAYMENT.SALE.DENIED":
            db_user.subscription_status = "past_due"
        elif event_type == "BILLING.SUBSCRIPTION.EXPIRED":
            db_user.subscription_status = "expired"

        db_user.subscription_updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"status": "processed"}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


def register_billing_routes(app):
    app.register_blueprint(billing_bp)
    print("[EDUAI-BILLING] ✅ Billing routes registered at /eduai/billing")
