"""
eduai/app/routes/subscription_gate.py
Decorator that blocks access once trial has expired and no active
subscription exists. Drop this on any route that should be paywalled.
"""

from datetime import datetime
from functools import wraps
from flask import jsonify
from eduai.app.database import get_db_direct
from eduai.app import models


def has_active_access(user: models.User) -> bool:
    """True if user is in trial window OR has an active paid subscription."""
    if user.subscription_status == "active":
        return True
    if user.subscription_status == "trial":
        if user.trial_ends_at and datetime.utcnow() < user.trial_ends_at:
            return True
    return False


def require_active_subscription(f):
    """
    Stack this AFTER @require_auth, e.g.:

        @app.route("/eduai/learner/learn", methods=["POST"])
        @require_auth
        @require_active_subscription
        def learn(user):
            ...

    Expects `user` as the first positional arg (same pattern as your
    existing require_auth in auth.py).
    """
    @wraps(f)
    def wrapper(user, *args, **kwargs):
        db = get_db_direct()
        try:
            fresh_user = db.query(models.User).filter_by(id=user.id).first()
            if not fresh_user:
                return jsonify({"error": "User not found"}), 404

            if not has_active_access(fresh_user):
                return jsonify({
                    "error": "subscription_required",
                    "message": "Your 7-day free trial has ended. Please subscribe to continue.",
                    "subscription_status": fresh_user.subscription_status,
                }), 402  # 402 Payment Required
            return f(user, *args, **kwargs)
        finally:
            db.close()
    return wrapper
