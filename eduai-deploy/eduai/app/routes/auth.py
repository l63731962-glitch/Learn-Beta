"""
eduai/app/routes/auth.py
─────────────────────────────────────────────────────────────────────────────
Standalone Flask Blueprint for EduAI authentication routes.
Mounted at /eduai/auth/* by register_auth_routes(app).

These routes mirror the auth section already inside eduai_routes.py so that
auth can optionally be registered independently (e.g. for testing or if you
ever split the blueprint). If you use the all-in-one eduai_routes.py, you do
NOT need to register this file separately — the /eduai/auth/* routes are
already covered. This file is provided for completeness and future modularity.

Routes:
    POST  /eduai/auth/register
    POST  /eduai/auth/login
    GET   /eduai/auth/me
    POST  /eduai/auth/logout
    POST  /eduai/auth/update-language
"""

from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify

from app.database import get_db_direct
from app import models
from app.service import auth_service

# ── Blueprint ──────────────────────────────────────────────────────────────
auth_bp = Blueprint("eduai_auth", __name__, url_prefix="/eduai/auth")


# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _get_token() -> str:
    """Extract Bearer token from Authorization header or JSON body."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    if request.is_json:
        return request.json.get("token", "")
    return ""


def _current_user(db=None):
    """
    Return (user, None) on success or (None, error_response) on failure.
    Caller must close db if they passed None (function opens its own).
    """
    owns_db = db is None
    if owns_db:
        db = get_db_direct()
    try:
        token = _get_token()
        if not token:
            return None, (jsonify({"error": "Unauthorized — no token"}), 401)

        token_data = auth_service.verify_token(token)
        if not token_data:
            return None, (jsonify({"error": "Token expired or invalid"}), 401)

        user = db.query(models.User).filter_by(id=token_data["user_id"]).first()
        if not user:
            return None, (jsonify({"error": "User not found"}), 404)

        return user, None
    finally:
        if owns_db:
            db.close()


def require_auth(f):
    """Decorator: injects authenticated user as first positional argument."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, err = _current_user()
        if err:
            return err
        return f(user, *args, **kwargs)
    return wrapper


def _user_dict(user: models.User) -> dict:
    return {
        "id":            user.id,
        "name":          user.name,
        "email":         user.email,
        "role":          user.role.value,
        "class_level":   user.class_level,
        "sub_class":     user.sub_class,
        "school":        user.school,
        "language":      user.language,
        "language_name": user.language_name,
        "streak":        user.streak,
        "points":        user.points,
        "quizzes_done":  user.quizzes_done,
        "created_at":    user.created_at.isoformat() if user.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# POST /eduai/auth/register
# ════════════════════════════════════════════════════════════════════════════

@auth_bp.route("/register", methods=["POST"])
def register():
    """
    Register a new teacher or learner.

    Body (JSON):
        name          str  required
        email         str  required
        password      str  required (min 4 chars)
        role          str  "teacher" | "learner"  (default "learner")
        class_level   str  optional  e.g. "primary"
        sub_class     str  optional  e.g. "primary-3"
        school        str  optional
        language      str  optional  e.g. "en"
        language_name str  optional  e.g. "English"

    Returns 201:
        { token: str, user: {...} }
    """
    data = request.get_json(force=True) or {}
    db   = get_db_direct()
    try:
        name        = (data.get("name") or "").strip()
        email       = (data.get("email") or "").strip().lower()
        password    = (data.get("password") or "").strip()
        role        = data.get("role", "learner")
        class_level = data.get("class_level", "")
        sub_class   = data.get("sub_class", "")
        school      = data.get("school", "")
        language    = data.get("language", "en")
        lang_name   = data.get("language_name", "English")

        # ── Validation ────────────────────────────────────────────────────
        if not name or not email or not password:
            return jsonify({"error": "name, email, and password are required"}), 400

        if len(password) < 4:
            return jsonify({"error": "Password must be at least 4 characters"}), 400

        if role not in ("teacher", "learner"):
            return jsonify({"error": "role must be 'teacher' or 'learner'"}), 400

        if db.query(models.User).filter_by(email=email).first():
            return jsonify({"error": "Email already registered"}), 409

        # ── Create user ───────────────────────────────────────────────────
        user = models.User(
            name          = name,
            email         = email,
            password_hash = auth_service.hash_password(password),
            role          = models.UserRole(role),
            class_level   = class_level,
            sub_class     = sub_class,
            school        = school,
            language      = language,
            language_name = lang_name,
            last_login    = datetime.utcnow(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        token = auth_service.create_token(user.id, user.role.value)
        return jsonify({"token": token, "user": _user_dict(user)}), 201

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# POST /eduai/auth/login
# ════════════════════════════════════════════════════════════════════════════

@auth_bp.route("/login", methods=["POST"])
def login():
    """
    Authenticate an existing user.

    Body (JSON):
        email    str  required
        password str  required

    Returns 200:
        { token: str, user: {...} }
    Returns 401:
        { error: "Invalid email or password" }
    """
    data = request.get_json(force=True) or {}
    db   = get_db_direct()
    try:
        email    = (data.get("email") or "").strip().lower()
        password = (data.get("password") or "").strip()

        if not email or not password:
            return jsonify({"error": "email and password are required"}), 400

        user = db.query(models.User).filter_by(email=email).first()

        if not user or not auth_service.verify_password(password, user.password_hash):
            return jsonify({"error": "Invalid email or password"}), 401

        user.last_login = datetime.utcnow()
        db.commit()

        token = auth_service.create_token(user.id, user.role.value)
        return jsonify({"token": token, "user": _user_dict(user)}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# GET /eduai/auth/me
# ════════════════════════════════════════════════════════════════════════════

@auth_bp.route("/me", methods=["GET"])
@require_auth
def me(user):
    """Return the currently authenticated user's profile."""
    return jsonify({"user": _user_dict(user)}), 200


# ════════════════════════════════════════════════════════════════════════════
# POST /eduai/auth/logout
# ════════════════════════════════════════════════════════════════════════════

@auth_bp.route("/logout", methods=["POST"])
def logout():
    """
    Revoke the current session token.
    Safe to call even without a valid token — always returns 200.
    """
    token = _get_token()
    if token:
        auth_service.revoke_token(token)
    return jsonify({"status": "logged out"}), 200


# ════════════════════════════════════════════════════════════════════════════
# POST /eduai/auth/update-language
# ════════════════════════════════════════════════════════════════════════════

@auth_bp.route("/update-language", methods=["POST"])
@require_auth
def update_language(user):
    """
    Update the authenticated user's preferred language.

    Body (JSON):
        language      str  e.g. "yo"
        language_name str  e.g. "Yoruba"
    """
    data = request.get_json(force=True) or {}
    db   = get_db_direct()
    try:
        user_db = db.query(models.User).filter_by(id=user.id).first()
        if not user_db:
            return jsonify({"error": "User not found"}), 404

        user_db.language      = data.get("language", user.language)
        user_db.language_name = data.get("language_name", user.language_name)
        db.commit()

        return jsonify({
            "status":        "updated",
            "language":      user_db.language,
            "language_name": user_db.language_name,
        }), 200

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# REGISTRATION HELPER
# ════════════════════════════════════════════════════════════════════════════

def register_auth_routes(app):
    """
    Attach this blueprint to the OMEGA Flask app.

    IMPORTANT: Only call this if you are NOT using the all-in-one
    eduai_routes.py (which already registers /eduai/auth/* routes).
    Calling both will cause a 'View function mapping is overwriting
    an existing endpoint' error.

    Usage in api.py or mains.py:

        try:
            from app.routes.auth import register_auth_routes
            register_auth_routes(app)
            print("[API] ✅ EduAI auth routes registered at /eduai/auth")
        except Exception as e:
            print(f"[API] auth routes skipped: {e}")
    """
    app.register_blueprint(auth_bp)
    print("[EDUAI-AUTH] ✅ Auth routes registered at /eduai/auth")