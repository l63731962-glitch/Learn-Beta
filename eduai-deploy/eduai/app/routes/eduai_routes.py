"""
eduai/app/routes/eduai_routes.py
─────────────────────────────────────────────────────────────────────────────
ALL EduAI Flask routes in ONE Blueprint.
Registered onto the existing OMEGA Flask app in api.py via:

    from eduai.app.routes.eduai_routes import register_eduai_routes
    register_eduai_routes(app)

URL prefix: /eduai
So the full routes are e.g. POST /eduai/auth/register

This file has NO external server — everything flows through OMEGA's Flask
app on port 5000 exactly as Michael requested.
"""

import os
import io
import json
import secrets
import base64
from datetime import datetime, timedelta
from functools import wraps
import smtplib
import random
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Blueprint, request, jsonify, Response, stream_with_context

# ── DB setup ──────────────────────────────────────────────────────────────────
from sqlalchemy.orm import joinedload
from eduai.app.database import get_db_direct, init_db
from eduai.app import models

# ── Services ──────────────────────────────────────────────────────────────────
from eduai.app.service import ai_service, auth_service

# ─────────────────────────────────────────────────────────────────────────────
blueprint = Blueprint("eduai", __name__, url_prefix="/eduai")

# ════════════════════════════════════════════════════════════════════════════
# OTP STORE  (in-memory dict, survives within a single Flask process)
# Format:  { email: { otp: "123456", expires: float(unix_ts), payload: {...} } }
# ════════════════════════════════════════════════════════════════════════════
_otp_store: dict = {}
_OTP_TTL = 600  # 10 minutes


def _send_otp_email(to_email: str, otp: str) -> bool:
    """Send OTP via Resend API. Falls back to SMTP if no Resend key."""
    import urllib.request

    resend_key = os.getenv("RESEND_API_KEY", "")

    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px;
                background:#ffffff;border-radius:16px;color:#111111;
                border:1px solid #e5e7eb;">
      <h2 style="color:#7c3aed;margin-bottom:8px;">🎓 EduAI Learn</h2>
      <p style="color:#6b7280;margin-bottom:24px;">Email Verification</p>
      <div style="background:#f5f3ff;border:2px solid #7c3aed;border-radius:12px;
                  padding:24px;text-align:center;margin-bottom:24px;">
        <p style="color:#374151;font-size:14px;margin-bottom:8px;">Your verification code</p>
        <div style="font-size:48px;font-weight:700;letter-spacing:12px;color:#7c3aed;">
          {otp}
        </div>
      </div>
      <p style="color:#374151;font-size:13px;">This code expires in <strong>10 minutes</strong>.</p>
      <p style="color:#9ca3af;font-size:13px;">If you didn't request this, ignore this email.</p>
    </div>
    """

    # ── Try Resend first ─────────────────────────────────────────────────

# ── Mailjet — HTTPS API, auto-validated sender, no domain needed ───────
    import base64
    mj_key, mj_secret = os.getenv("MJ_APIKEY_PUBLIC", ""), os.getenv("MJ_APIKEY_PRIVATE", "")
    if mj_key and mj_secret:
        try:
            payload = json.dumps({
                "Messages": [{
                    "From":    {"Email": os.getenv("MJ_SENDER_EMAIL", ""), "Name": "EduAI Learn"},
                    "To":      [{"Email": to_email}],
                    "Subject": "Your EduAI Verification Code",
                    "HTMLPart": html,
                }]
            }).encode("utf-8")
            auth = base64.b64encode(f"{mj_key}:{mj_secret}".encode()).decode()
            req = urllib.request.Request(
                "https://api.mailjet.com/v3.1/send",
                data=payload,
                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                json.loads(resp.read())
                print("[EDUAI-OTP] ✅ Mailjet sent")
                return True
        except Exception as e:
            print(f"[EDUAI-OTP] ❌ Mailjet failed: {e}")  # falls through to Resend/SMTP

    if resend_key:
        try:
            payload = json.dumps({
                "from": "EduAI Learn <onboarding@resend.dev>",
                "to":   [to_email],
                "subject": "Your EduAI Verification Code",
                "html": html,
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type":  "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                print(f"[EDUAI-OTP] ✅ Resend sent: {result.get('id')}")
                return True
        except Exception as e:
            print(f"[EDUAI-OTP] ❌ Resend failed: {e}")
            return False

    # ── Fallback: Gmail SMTP ──────────────────────────────────────────────
    smtp_user = os.getenv("EDUAI_SMTP_USER", "")
    smtp_pass = os.getenv("EDUAI_SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        print(f"[EDUAI-OTP] ⚠️  No email provider configured — OTP for {to_email}: {otp}")
        return True  # dev fallback: log it

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your EduAI Verification Code"
        msg["From"]    = smtp_user
        msg["To"]      = to_email
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"[EDUAI-OTP] ❌ SMTP failed: {e}")
        return False

# Ensure tables exist when this blueprint is first imported
try:
    init_db()
except Exception as e:
    print(f"[EDUAI-ROUTES] DB init warning: {e}")


# ════════════════════════════════════════════════════════════════════════════
# AUTH HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _get_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.json.get("token", "") if request.is_json else ""


def _current_user(db=None):
    """Return (user_obj, error_response).  One of them is always None."""
    close_db = False
    if db is None:
        db, close_db = get_db_direct(), True
    try:
        token = _get_token()
        if not token:
            return None, (jsonify({"error": "Unauthorized — no token"}), 401)
        td = auth_service.verify_token(token)
        if not td:
            return None, (jsonify({"error": "Token expired or invalid"}), 401)
        user = (
            db.query(models.User)
            .options(joinedload(models.User.organization))
            .filter_by(id=td["user_id"])
            .first()
        )
        if not user:
            return None, (jsonify({"error": "User not found"}), 404)
        return user, None
    finally:
        if close_db:
            db.close()


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, err = _current_user()
        if err:
            return err
        return f(user, *args, **kwargs)
    return wrapper


def require_teacher(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, err = _current_user()
        if err:
            return err
        if user.role != models.UserRole.teacher:
            return jsonify({"error": "Teacher access required"}), 403
        return f(user, *args, **kwargs)
    return wrapper


def require_learner(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, err = _current_user()
        if err:
            return err
        if user.role != models.UserRole.learner:
            return jsonify({"error": "Learner access required"}), 403
        return f(user, *args, **kwargs)
    return wrapper
def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, err = _current_user()
        if err:
            return err
        if not user.is_admin:
            return jsonify({"error": "Admin access required"}), 403
        return f(user, *args, **kwargs)
    return wrapper


def require_active_subscription(f):
    """Stack directly under @require_auth / @require_teacher / @require_learner.
    Blocks access once the 7-day trial has ended and no active PayPal
    subscription exists. Admin routes are intentionally NOT gated with this."""
    @wraps(f)
    def wrapper(user, *args, **kwargs):
        db = get_db_direct()
        try:
            fresh_user = db.query(models.User).filter_by(id=user.id).first()
            if not fresh_user:
                return jsonify({"error": "User not found"}), 404
            active = False
            if fresh_user.subscription_status == "active":
                active = True
            elif fresh_user.subscription_status == "trial":
                if fresh_user.trial_ends_at and datetime.utcnow() < fresh_user.trial_ends_at:
                    active = True
            if not active:
                return jsonify({
                    "error": "subscription_required",
                    "message": "Your 7-day free trial has ended. Please subscribe to continue.",
                    "subscription_status": fresh_user.subscription_status,
                }), 402
            return f(user, *args, **kwargs)
        finally:
            db.close()
    return wrapper


def _gen_code(prefix: str) -> str:
    """Generate e.g. ADM-A1B2-C3D4 or SCH-A1B2-C3D4"""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
    part1 = "".join(secrets.choice(chars) for _ in range(4))
    part2 = "".join(secrets.choice(chars) for _ in range(4))
    return f"{prefix}-{part1}-{part2}"
    


# ════════════════════════════════════════════════════════════════════════════
# ── AUTH ─────────────────────────────────────────────────────────────────────
# POST /eduai/auth/register
# POST /eduai/auth/login
# GET  /eduai/auth/me
# POST /eduai/auth/logout
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
# POST /eduai/auth/send-otp
# Called BEFORE account creation. Validates email uniqueness, sends OTP.
# ════════════════════════════════════════════════════════════════════════════
@blueprint.route("/auth/send-otp", methods=["POST"])
def auth_send_otp():
    data  = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    db    = get_db_direct()
    try:
        if not email:
            return jsonify({"error": "email is required"}), 400

        # Basic format check
        import re
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return jsonify({"error": "Invalid email format"}), 400

        # Check already registered
        if db.query(models.User).filter_by(email=email).first():
            return jsonify({"error": "Email already registered"}), 409

        # Generate 6-digit OTP
        otp = str(random.randint(100000, 999999))

        # Store full registration payload alongside OTP so /verify-otp can create the user
        _otp_store[email] = {
            "otp":     otp,
            "expires": time.time() + _OTP_TTL,
            "payload": data.get("reg_payload", {}),   # frontend passes full payload
        }

        # Send email
        sent = _send_otp_email(email, otp)
        if not sent:
            return jsonify({"error": "Failed to send verification email. Try again."}), 500

        return jsonify({"status": "otp_sent", "email": email}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# POST /eduai/auth/verify-otp
# Verifies OTP then creates the account (moves logic from /register).
# ════════════════════════════════════════════════════════════════════════════
@blueprint.route("/auth/verify-otp", methods=["POST"])
def auth_verify_otp():
    data  = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    otp   = (data.get("otp") or "").strip()
    db    = get_db_direct()
    try:
        if not email or not otp:
            return jsonify({"error": "email and otp are required"}), 400

        entry = _otp_store.get(email)
        if not entry:
            return jsonify({"error": "No OTP found for this email. Please request a new code."}), 400
        if time.time() > entry["expires"]:
            _otp_store.pop(email, None)
            return jsonify({"error": "OTP has expired. Please request a new code."}), 400
        if entry["otp"] != otp:
            return jsonify({"error": "Incorrect verification code. Try again."}), 400

        # OTP valid — retrieve saved payload and create the account
        reg = entry["payload"]
        _otp_store.pop(email, None)   # consume OTP

        name         = (reg.get("name") or "").strip()
        password     = (reg.get("password") or "").strip()
        role         = reg.get("role", "learner")
        class_level  = reg.get("class_level", "")
        sub_class    = reg.get("sub_class", "")
        school       = reg.get("school", "")
        language     = reg.get("language", "en")
        lang_name    = reg.get("language_name", "English")
        account_type = reg.get("account_type", "personal")
        org_mode     = reg.get("org_mode")
        school_name  = (reg.get("school_name") or "").strip()
        referral_code= (reg.get("referral_code") or "").strip().upper()

        if not name or not password:
            return jsonify({"error": "Registration payload incomplete. Please start over."}), 400
        if role not in ("teacher", "learner"):
            return jsonify({"error": "Invalid role"}), 400
        if account_type not in ("personal", "organization"):
            return jsonify({"error": "Invalid account_type"}), 400
        if db.query(models.User).filter_by(email=email).first():
            return jsonify({"error": "Email already registered"}), 409

        org_id    = None
        is_admin  = False
        org_codes = None
        new_org   = None

        if account_type == "organization":
            if role == "teacher" and org_mode == "create":
                if not school_name:
                    return jsonify({"error": "school_name is required"}), 400
                new_org = models.Organization(
                    name=school_name,
                    admin_code=_gen_code("ADM"),
                    referral_code=_gen_code("SCH"),
                )
                db.add(new_org)
                db.flush()
                org_id    = new_org.id
                is_admin  = True
                org_codes = {"admin_code": new_org.admin_code, "referral_code": new_org.referral_code}
            elif (role == "teacher" and org_mode == "join") or role == "learner":
                if not referral_code:
                    return jsonify({"error": "referral_code is required"}), 400
                org = db.query(models.Organization).filter_by(referral_code=referral_code).first()
                if not org:
                    return jsonify({"error": "invalid_referral_code", "message": "Invalid referral code"}), 400
                org_id = org.id
            else:
                return jsonify({"error": "Invalid org_mode for this role"}), 400

        user = models.User(
            name=name, email=email,
            password_hash=auth_service.hash_password(password),
            role=models.UserRole(role),
            class_level=class_level, sub_class=sub_class,
            school=school, language=language, language_name=lang_name,
            last_login=datetime.utcnow(),
            account_type=models.AccountType(account_type),
            org_id=org_id,
            is_admin=is_admin,
            subscription_status="trial",
            trial_started_at=datetime.utcnow(),
            trial_ends_at=datetime.utcnow() + timedelta(days=7),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        if new_org and not new_org.created_by:
            new_org.created_by = user.id
            db.commit()

        token = auth_service.create_token(user.id, user.role.value)
        resp  = {"token": token, "user": _user_dict(user)}
        if org_codes:
            resp["org"] = org_codes
        return jsonify(resp), 201

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()

@blueprint.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(force=True)
    db   = get_db_direct()
    try:
        name         = (data.get("name") or "").strip()
        email        = (data.get("email") or "").strip().lower()
        password     = (data.get("password") or "").strip()
        role         = data.get("role", "learner")
        class_level  = data.get("class_level", "")
        sub_class    = data.get("sub_class", "")
        school       = data.get("school", "")
        language     = data.get("language", "en")
        lang_name    = data.get("language_name", "English")
        account_type = data.get("account_type", "personal")
        org_mode     = data.get("org_mode")          # 'create' | 'join'
        school_name  = (data.get("school_name") or "").strip()
        referral_code= (data.get("referral_code") or "").strip().upper()

        if not name or not email or not password:
            return jsonify({"error": "name, email, and password are required"}), 400
        if len(password) < 4:
            return jsonify({"error": "Password must be at least 4 characters"}), 400
        if role not in ("teacher", "learner"):
            return jsonify({"error": "role must be 'teacher' or 'learner'"}), 400
        if account_type not in ("personal", "organization"):
            return jsonify({"error": "account_type must be 'personal' or 'organization'"}), 400
        if db.query(models.User).filter_by(email=email).first():
            return jsonify({"error": "Email already registered"}), 409

        org_id      = None
        is_admin    = False
        org_codes   = None
        new_org     = None

        if account_type == "organization":
            if role == "teacher" and org_mode == "create":
                if not school_name:
                    return jsonify({"error": "school_name is required to create a school"}), 400
                new_org = models.Organization(
                    name=school_name,
                    admin_code=_gen_code("ADM"),
                    referral_code=_gen_code("SCH"),
                )
                db.add(new_org)
                db.flush()  # get new_org.id without full commit
                org_id   = new_org.id
                is_admin = True
                org_codes = {"admin_code": new_org.admin_code, "referral_code": new_org.referral_code}

            elif (role == "teacher" and org_mode == "join") or role == "learner":
                if not referral_code:
                    return jsonify({"error": "referral_code is required"}), 400
                org = db.query(models.Organization).filter_by(referral_code=referral_code).first()
                if not org:
                    return jsonify({
                        "error": "invalid_referral_code",
                        "message": "Invalid referral code — check with your school admin"
                    }), 400
                org_id   = org.id
                is_admin = False
            else:
                return jsonify({"error": "Invalid org_mode for this role"}), 400

        user = models.User(
            name=name, email=email,
            password_hash=auth_service.hash_password(password),
            role=models.UserRole(role),
            class_level=class_level, sub_class=sub_class,
            school=school, language=language, language_name=lang_name,
            last_login=datetime.utcnow(),
            account_type=models.AccountType(account_type),
            org_id=org_id,
            is_admin=is_admin,
            subscription_status="trial",
            trial_started_at=datetime.utcnow(),
            trial_ends_at=datetime.utcnow() + timedelta(days=7),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        if new_org and not new_org.created_by:
            new_org.created_by = user.id
            db.commit()

        token = auth_service.create_token(user.id, user.role.value)
        resp = {"token": token, "user": _user_dict(user)}
        if org_codes:
            resp["org"] = org_codes
        return jsonify(resp), 201

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/auth/login", methods=["POST"])
def auth_login():
    data  = request.get_json(force=True)
    db    = get_db_direct()
    try:
        email    = (data.get("email") or "").strip().lower()
        password = (data.get("password") or "").strip()
        user     = db.query(models.User).filter_by(email=email).first()
        if not user or not auth_service.verify_password(password, user.password_hash):
            return jsonify({"error": "Invalid email or password"}), 401
        user.last_login  = datetime.utcnow()
        user.last_active = datetime.utcnow()
        db.commit()
        token = auth_service.create_token(user.id, user.role.value)
        return jsonify({"token": token, "user": _user_dict(user)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/auth/me", methods=["GET"])
@require_auth
def auth_me(user):
    return jsonify({"user": _user_dict(user)})


@blueprint.route("/auth/logout", methods=["POST"])
def auth_logout():
    token = _get_token()
    if token:
        auth_service.revoke_token(token)
    return jsonify({"status": "logged out"})


@blueprint.route("/auth/update-language", methods=["POST"])
@require_auth
def update_language(user):
    data = request.get_json(force=True)
    db   = get_db_direct()
    try:
        user_db = db.query(models.User).filter_by(id=user.id).first()
        user_db.language      = data.get("language", user.language)
        user_db.language_name = data.get("language_name", user.language_name)
        db.commit()
        return jsonify({"status": "updated", "language": user_db.language})
    finally:
        db.close()

# ── Rate-limit store for check-referral (in-memory, TODO: move to Redis in prod) ─
_referral_rate_limit: dict = {}   # {ip: [timestamps]}

@blueprint.route("/auth/check-referral", methods=["GET"])
def check_referral():
    import time
    ip  = request.remote_addr or "unknown"
    now = time.time()
    window = 600  # 10 minutes
    max_req = 5

    hits = [t for t in _referral_rate_limit.get(ip, []) if now - t < window]
    if len(hits) >= max_req:
        return jsonify({"valid": False, "error": "rate_limited"}), 429
    hits.append(now)
    _referral_rate_limit[ip] = hits

    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"valid": False}), 200
    db = get_db_direct()
    try:
        org = db.query(models.Organization).filter_by(referral_code=code).first()
        if org:
            return jsonify({"valid": True, "org_name": org.name}), 200
        return jsonify({"valid": False}), 200
    finally:
        db.close()

@blueprint.route("/auth/my-org-codes", methods=["GET"])
@require_admin
def my_org_codes(user):
    db = get_db_direct()
    try:
        org = db.query(models.Organization).filter_by(id=user.org_id).first()
        if not org:
            return jsonify({"error": "No organization found"}), 404
        return jsonify({"admin_code": org.admin_code, "referral_code": org.referral_code}), 200
    finally:
        db.close()

def _user_dict(user) -> dict:
    org_name = user.organization.name if user.organization else None
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
        "account_type":  user.account_type.value if user.account_type else "personal",
        "org_id":        user.org_id,
        "org_name":      org_name,
      "is_admin":      bool(user.is_admin),
        "last_active":   user.last_active.isoformat() if user.last_active else None,
    }

# ════════════════════════════════════════════════════════════════════════════
# ── TEACHER — LESSON NOTES ────────────────────────────────────────────────
# POST /eduai/teacher/lesson/generate          → streaming SSE
# POST /eduai/teacher/lesson/generate-sync     → full JSON
# GET  /eduai/teacher/lesson/list
# POST /eduai/teacher/lesson/save
# DELETE /eduai/teacher/lesson/<id>
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/teacher/lesson/generate", methods=["POST"])
@require_teacher
@require_active_subscription
def lesson_generate_stream(user):
    """Server-Sent Events stream of the lesson note as it generates."""
    data = request.get_json(force=True)

    subject     = data.get("subject", "")
    class_level = data.get("class_level", "primary")
    sub_class   = data.get("sub_class", "")
    topic       = data.get("topic", "")
    duration    = data.get("duration", "45 minutes")
    curriculum  = data.get("curriculum", "Nigerian (NERDC)")
    language    = data.get("language_name", user.language_name or "English")

    if not subject or not topic:
        return jsonify({"error": "subject and topic are required"}), 400

    def generate():
        full = ""
        try:
            for delta in ai_service.generate_lesson_note(
                subject=subject, class_level=class_level, sub_class=sub_class,
                topic=topic, duration=duration, curriculum=curriculum,
                language=language, stream=True,
            ):
                full += delta
                yield f"data: {json.dumps({'delta': delta, 'full': full})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield f"data: {json.dumps({'done': True, 'full': full})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@blueprint.route("/teacher/lesson/generate-sync", methods=["POST"])
@require_teacher
@require_active_subscription
def lesson_generate_sync(user):
    """Non-streaming lesson note generation — returns full content in one response."""
    data = request.get_json(force=True)

    subject     = data.get("subject", "")
    class_level = data.get("class_level", "primary")
    sub_class   = data.get("sub_class", "")
    topic       = data.get("topic", "")
    duration    = data.get("duration", "45 minutes")
    curriculum  = data.get("curriculum", "Nigerian (NERDC)")
    language    = data.get("language_name", user.language_name or "English")

    if not subject or not topic:
        return jsonify({"error": "subject and topic are required"}), 400

    content = ai_service.generate_lesson_note(
        subject=subject, class_level=class_level, sub_class=sub_class,
        topic=topic, duration=duration, curriculum=curriculum,
        language=language, stream=False,
    )
    if not content:
        return jsonify({"error": "AI generation failed — check API keys"}), 500

    return jsonify({"content": content, "topic": topic, "language": language})


@blueprint.route("/teacher/lesson/save", methods=["POST"])
@require_teacher
@require_active_subscription
def lesson_save(user):
    data = request.get_json(force=True)
    db   = get_db_direct()
    try:
        note = models.LessonNote(
            teacher_id   = user.id,
            subject      = data.get("subject", ""),
            class_level  = data.get("class_level", ""),
            sub_class    = data.get("sub_class", ""),
            topic        = data.get("topic", ""),
            duration     = data.get("duration", "45 minutes"),
            curriculum   = data.get("curriculum", "Nigerian (NERDC)"),
            language     = data.get("language", "en"),
            language_name= data.get("language_name", "English"),
            content      = data.get("content", ""),
        )
        db.add(note)
        db.commit()
        db.refresh(note)
        return jsonify({"status": "saved", "id": note.id}), 201
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/teacher/lesson/list", methods=["GET"])
@require_teacher
@require_active_subscription
def lesson_list(user):
    db = get_db_direct()
    try:
        notes = (
            db.query(models.LessonNote)
            .filter_by(teacher_id=user.id)
            .order_by(models.LessonNote.created_at.desc())
            .limit(50)
            .all()
        )
        return jsonify({"lessons": [_note_dict(n) for n in notes]})
    finally:
        db.close()


@blueprint.route("/teacher/lesson/<int:note_id>", methods=["DELETE"])
@require_teacher
@require_active_subscription
def lesson_delete(user, note_id):
    db = get_db_direct()
    try:
        note = db.query(models.LessonNote).filter_by(id=note_id, teacher_id=user.id).first()
        if not note:
            return jsonify({"error": "Not found"}), 404
        db.delete(note)
        db.commit()
        return jsonify({"status": "deleted"})
    finally:
        db.close()


def _note_dict(n) -> dict:
    return {
        "id":          n.id,
        "subject":     n.subject,
        "class_level": n.class_level,
        "sub_class":   n.sub_class,
        "topic":       n.topic,
        "duration":    n.duration,
        "curriculum":  n.curriculum,
        "language":    n.language_name,
        "content":     n.content,
        "created_at":  n.created_at.isoformat() if n.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# ── TEACHER — SCHEME OF WORK ──────────────────────────────────────────────
# POST /eduai/teacher/sow/extract      → extract/generate topics
# POST /eduai/teacher/sow/generate-all → generate all lesson notes (SSE)
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/teacher/sow/extract", methods=["POST"])
@require_teacher
@require_active_subscription
def sow_extract(user):
    """Extract topics from an uploaded file or generate a 12-week SOW."""
    subject    = request.form.get("subject", "General")
    class_level= request.form.get("class_level", "primary")
    curriculum = request.form.get("curriculum", "Nigerian (NERDC)")
    file_text  = ""

    f = request.files.get("file")
    if f:
        raw = f.read()
        # Try plain text decode
        try:
            file_text = raw.decode("utf-8", errors="ignore")[:8000]
        except Exception:
            pass
        # Try PDF extract
        if f.filename.lower().endswith(".pdf") and not file_text.strip():
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(io.BytesIO(raw))
                file_text = "\n".join(
                    p.extract_text() for p in reader.pages[:15] if p.extract_text()
                )[:8000]
            except Exception:
                pass

    topics = ai_service.extract_sow_topics(
        subject=subject, class_level=class_level,
        curriculum=curriculum, file_text=file_text,
    )
    if topics is None:
        return jsonify({"error": "AI failed to extract topics — check API keys"}), 500

    # Persist SOW record
    db = get_db_direct()
    try:
        sow = models.SchemeOfWork(
            teacher_id  = user.id,
            subject     = subject,
            class_level = class_level,
            curriculum  = curriculum,
            filename    = f.filename if f else None,
            topics      = topics,
        )
        db.add(sow)
        db.commit()
        db.refresh(sow)
        return jsonify({"sow_id": sow.id, "topics": topics})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/teacher/sow/generate-all", methods=["POST"])
@require_teacher
@require_active_subscription
def sow_generate_all(user):
    """
    SSE stream that generates one lesson note per topic and yields progress.
    Body: { sow_id, topics:[{week,topic,objectives}], subject, class_level, curriculum, language_name }
    """
    data       = request.get_json(force=True)
    topics     = data.get("topics", [])
    subject    = data.get("subject", "General")
    class_level= data.get("class_level", "primary")
    curriculum = data.get("curriculum", "Nigerian (NERDC)")
    language   = data.get("language_name", user.language_name or "English")
    sow_id     = data.get("sow_id")

    if not topics:
        return jsonify({"error": "No topics provided"}), 400

    def generate():
        results = []
        total   = len(topics)
        for i, t in enumerate(topics):
            topic_name = t.get("topic", f"Week {t.get('week', i+1)}")
            yield f"data: {json.dumps({'progress': i, 'total': total, 'topic': topic_name, 'status': 'generating'})}\n\n"

            content = ai_service.generate_lesson_note(
                subject=subject, class_level=class_level,
                sub_class="", topic=topic_name,
                duration="45 minutes", curriculum=curriculum,
                language=language, stream=False,
            )
            if content:
                results.append({"week": t.get("week", i + 1), "topic": topic_name, "content": content})
                yield f"data: {json.dumps({'progress': i + 1, 'total': total, 'topic': topic_name, 'status': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'progress': i + 1, 'total': total, 'topic': topic_name, 'status': 'failed'})}\n\n"

        # Save notes to DB
        db = get_db_direct()
        try:
            for r in results:
                note = models.LessonNote(
                    teacher_id=user.id, subject=subject, class_level=class_level,
                    sub_class="", topic=r["topic"], duration="45 minutes",
                    curriculum=curriculum, language=user.language,
                    language_name=language, content=r["content"],
                )
                db.add(note)
            if sow_id:
                sow = db.query(models.SchemeOfWork).filter_by(id=sow_id, teacher_id=user.id).first()
                if sow:
                    sow.notes_count = len(results)
            db.commit()
        except Exception as e:
            db.rollback()
        finally:
            db.close()

        yield f"data: {json.dumps({'done': True, 'generated': len(results), 'total': total})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ════════════════════════════════════════════════════════════════════════════
# ── TEACHER — TEST GENERATOR ───────────────────────────────────────────────
# POST /eduai/teacher/test/generate
# POST /eduai/teacher/test/submit-result
# GET  /eduai/teacher/test/sessions
# GET  /eduai/teacher/test/results/<session_id>
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/teacher/test/generate", methods=["POST"])
@require_teacher
@require_active_subscription
def test_generate(user):
    data = request.get_json(force=True)

    subject    = data.get("subject", "")
    class_level= data.get("class_level", "primary")
    sub_class  = data.get("sub_class", "")
    topic      = data.get("topic", "")
    num_q      = min(int(data.get("num_questions", 20)), 100)
    difficulty = data.get("difficulty", "Medium")
    q_types    = data.get("question_types", ["Multiple Choice (4 options A B C D)"])
    duration   = int(data.get("duration_min", 60))
    fmt        = data.get("format", "cbt")
    language   = data.get("language_name", user.language_name or "English")

    if not subject:
        return jsonify({"error": "subject is required"}), 400

    raw = ai_service.generate_test_questions(
        subject=subject, class_level=class_level, sub_class=sub_class,
        topic=topic, num_q=num_q, difficulty=difficulty,
        question_types=q_types, language=language,
    )
    if not raw:
        return jsonify({"error": "AI generation failed — check API keys"}), 500

    questions = ai_service.parse_test_questions(raw, max_q=num_q)
    pin_code  = secrets.token_hex(3).upper()   # 6-char hex PIN for CBT sessions

    db = get_db_direct()
    try:
        session = models.TestSession(
            teacher_id   = user.id,
            subject      = subject,
            class_level  = class_level,
            sub_class    = sub_class,
            topic        = topic,
            num_questions= len(questions),
            difficulty   = difficulty,
            duration_min = duration,
            format       = fmt,
            language     = user.language,
            questions    = questions,
            raw_content  = raw,
            pin_code     = pin_code,
        )
        db.add(session)
        db.commit()
        db.refresh(session)

        return jsonify({
            "session_id": session.id,
            "pin_code":   pin_code,
            "questions":  questions,
            "raw":        raw,
            "num_generated": len(questions),
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/teacher/test/submit-result", methods=["POST"])
def test_submit_result():
    """
    Submit a student's CBT result.
    No auth required — students don't have accounts.
    Body: { session_id, student_name, student_class, adm_number, gender, answers:{q_idx:letter} }
    """
    data = request.get_json(force=True)
    db   = get_db_direct()
    try:
        session_id = data.get("session_id")
        session    = db.query(models.TestSession).filter_by(id=session_id).first()
        if not session:
            return jsonify({"error": "Test session not found"}), 404

        answers    = data.get("answers", {})
        questions  = session.questions or []
        correct    = 0

        for i, q in enumerate(questions):
            given = answers.get(str(i)) or answers.get(i)
            right = None
            for opt in (q.get("opts") or []):
                if opt.get("correct"):
                    right = opt.get("letter")
                    break
            if not right:
                right = q.get("ans")
            if given and given == right:
                correct += 1

        total     = len(questions)
        pct       = round((correct / total) * 100, 1) if total else 0
        score_raw = f"{correct}/{total}"

        result = models.TestResult(
            session_id    = session_id,
            student_name  = data.get("student_name", "Student"),
            student_class = data.get("student_class", ""),
            adm_number    = data.get("adm_number", ""),
            gender        = data.get("gender", ""),
            score_pct     = pct,
            score_raw     = score_raw,
            answers       = answers,
            time_taken_s  = data.get("time_taken_s"),
        )
        db.add(result)

        # Update leaderboard — scoped to the teacher's org
        teacher = db.query(models.User).filter_by(id=session.teacher_id).first()
        lb = models.LeaderboardEntry(
            learner_name = data.get("student_name", "Student"),
            org_id       = teacher.org_id if teacher else None,
            score_pct    = pct,
            score_raw    = score_raw,
            subject      = session.subject,
            class_level  = session.class_level,
            entry_type   = "cbt",
        )
        db.add(lb)
        db.commit()

        return jsonify({
            "score_pct":  pct,
            "score_raw":  score_raw,
            "correct":    correct,
            "total":      total,
            "grade":      _grade(pct),
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/teacher/test/sessions", methods=["GET"])
@require_teacher
@require_active_subscription
def test_sessions(user):
    db = get_db_direct()
    try:
        sessions = (
            db.query(models.TestSession)
            .filter_by(teacher_id=user.id)
            .order_by(models.TestSession.created_at.desc())
            .limit(30)
            .all()
        )
        return jsonify({"sessions": [_session_dict(s) for s in sessions]})
    finally:
        db.close()


@blueprint.route("/teacher/test/results/<int:session_id>", methods=["GET"])
@require_teacher
@require_active_subscription
def test_results(user, session_id):
    db = get_db_direct()
    try:
        session = db.query(models.TestSession).filter_by(id=session_id, teacher_id=user.id).first()
        if not session:
            return jsonify({"error": "Not found"}), 404
        results = db.query(models.TestResult).filter_by(session_id=session_id).all()
        avg     = sum(r.score_pct for r in results) / len(results) if results else 0
        return jsonify({
            "session":  _session_dict(session),
            "results":  [_result_dict(r) for r in results],
            "summary":  {
                "count":   len(results),
                "average": round(avg, 1),
                "highest": max((r.score_pct for r in results), default=0),
                "lowest":  min((r.score_pct for r in results), default=0),
            },
        })
    finally:
        db.close()


@blueprint.route("/teacher/stats", methods=["GET"])
@require_teacher
@require_active_subscription
def teacher_stats(user):
    db = get_db_direct()
    try:
        lessons  = db.query(models.LessonNote).filter_by(teacher_id=user.id).count()
        sessions = db.query(models.TestSession).filter_by(teacher_id=user.id).all()
        tests    = len(sessions)
        all_results = [r for s in sessions for r in s.results]
        students = len(all_results)
        avg      = round(sum(r.score_pct for r in all_results) / students, 1) if students else None
        return jsonify({
            "lessons":  lessons,
            "tests":    tests,
            "students": students,
            "avg_score": avg,
        })
    finally:
        db.close()


def _session_dict(s) -> dict:
    return {
        "id":           s.id,
        "subject":      s.subject,
        "class_level":  s.class_level,
        "sub_class":    s.sub_class,
        "topic":        s.topic,
        "num_questions":s.num_questions,
        "difficulty":   s.difficulty,
        "duration_min": s.duration_min,
        "format":       s.format,
        "pin_code":     s.pin_code,
        "created_at":   s.created_at.isoformat() if s.created_at else None,
    }


def _result_dict(r) -> dict:
    return {
        "id":            r.id,
        "student_name":  r.student_name,
        "student_class": r.student_class,
        "adm_number":    r.adm_number,
        "gender":        r.gender,
        "score_pct":     r.score_pct,
        "score_raw":     r.score_raw,
        "time_taken_s":  r.time_taken_s,
        "submitted_at":  r.submitted_at.isoformat() if r.submitted_at else None,
    }


def _grade(pct: float) -> str:
    if pct >= 90: return "A+"
    if pct >= 80: return "A"
    if pct >= 70: return "B"
    if pct >= 60: return "C"
    if pct >= 50: return "D"
    return "F"


# ════════════════════════════════════════════════════════════════════════════
# ── TEACHER — PERFORMANCE DASHBOARD ──────────────────────────────────────
# GET /eduai/teacher/performance
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/teacher/performance", methods=["GET"])
@require_teacher
@require_active_subscription
def teacher_performance(user):
    db = get_db_direct()
    try:
        sessions = (
            db.query(models.TestSession)
            .filter_by(teacher_id=user.id)
            .all()
        )
        all_results = [r for s in sessions for r in s.results]
        if not all_results:
            return jsonify({"summary": None, "insight": None, "sessions": []})

        scores  = [r.score_pct for r in all_results]
        avg     = sum(scores) / len(scores)
        highest = max(scores)
        lowest  = min(scores)

        # Identify the session with lowest average
        session_avgs = {}
        for s in sessions:
            if s.results:
                session_avgs[s.subject or s.topic or "General"] = (
                    sum(r.score_pct for r in s.results) / len(s.results)
                )
        weak = sorted(session_avgs, key=session_avgs.get)[:3]

        insight = ai_service.generate_performance_insight(
            subject      = "All subjects",
            avg_score    = avg,
            weak_topics  = weak,
            num_students = len(all_results),
            language     = user.language_name or "English",
        )

        return jsonify({
            "summary": {
                "total_results": len(all_results),
                "average":       round(avg, 1),
                "highest":       highest,
                "lowest":        lowest,
                "weak_areas":    weak,
            },
            "insight":  insight,
            "sessions": [_session_dict(s) for s in sessions],
            "results":  [_result_dict(r) for r in all_results],
        })
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# ── LEARNER — LEARN ANYTHING ──────────────────────────────────────────────
# POST /eduai/learner/learn          → SSE stream
# POST /eduai/learner/learn-sync     → full JSON
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/learner/learn", methods=["POST"])
@require_learner
@require_active_subscription
def learner_learn_stream(user):
    data    = request.get_json(force=True)
    topic   = data.get("topic", "").strip()
    subject = data.get("subject", "")
    language= data.get("language_name", user.language_name or "English")

    if not topic:
        return jsonify({"error": "topic is required"}), 400

    def generate():
        full = ""
        try:
            for delta in ai_service.explain_topic(
                topic=topic, subject=subject,
                class_level=user.class_level or "primary",
                sub_class=user.sub_class or "",
                language=language, stream=True,
            ):
                full += delta
                yield f"data: {json.dumps({'delta': delta, 'full': full})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield f"data: {json.dumps({'done': True, 'full': full})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@blueprint.route("/learner/learn-sync", methods=["POST"])
@require_learner
@require_active_subscription
def learner_learn_sync(user):
    data    = request.get_json(force=True)
    topic   = data.get("topic", "").strip()
    subject = data.get("subject", "")
    language= data.get("language_name", user.language_name or "English")

    if not topic:
        return jsonify({"error": "topic is required"}), 400

    content = ai_service.explain_topic(
        topic=topic, subject=subject,
        class_level=user.class_level or "primary",
        sub_class=user.sub_class or "",
        language=language, stream=False,
    )
    if not content:
        return jsonify({"error": "AI generation failed"}), 500

    # Award points
    db = get_db_direct()
    try:
        u = db.query(models.User).filter_by(id=user.id).first()
        if u:
            u.points = (u.points or 0) + 10
            db.commit()
    except Exception:
        pass
    finally:
        db.close()

    return jsonify({"content": content, "topic": topic})


# ════════════════════════════════════════════════════════════════════════════
# ── LEARNER — QUIZ ────────────────────────────────────────────────────────
# POST /eduai/learner/quiz/generate
# POST /eduai/learner/quiz/submit
# GET  /eduai/learner/quiz/history
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/learner/quiz/generate", methods=["POST"])
@require_learner
@require_active_subscription
def quiz_generate(user):
    data       = request.get_json(force=True)
    subject    = data.get("subject", "General Knowledge")
    topic      = data.get("topic", "")
    num_q      = min(int(data.get("num_questions", 10)), 50)
    difficulty = data.get("difficulty", "Medium")
    language   = data.get("language_name", user.language_name or "English")

    questions = ai_service.generate_quiz(
        subject=subject, topic=topic,
        class_level=user.class_level or "primary",
        sub_class=user.sub_class or "",
        num_q=num_q, difficulty=difficulty,
        language=language,
    )
    if questions is None:
        return jsonify({"error": "AI quiz generation failed — check API keys"}), 500

    return jsonify({
        "questions": questions,
        "num_generated": len(questions),
    })


@blueprint.route("/learner/quiz/submit", methods=["POST"])
@require_learner
@require_active_subscription
def quiz_submit(user):
    """
    Save quiz result and update leaderboard + user points.
    Body: { subject, topic, questions:[...], answers:[int,...], mode, difficulty }
    """
    data      = request.get_json(force=True)
    questions = data.get("questions", [])
    answers   = data.get("answers", [])  # list of chosen option indices
    subject   = data.get("subject", "")
    topic     = data.get("topic", "")
    mode      = data.get("mode", "practice")
    difficulty= data.get("difficulty", "Medium")

    correct = sum(
        1 for i, q in enumerate(questions)
        if i < len(answers) and answers[i] == q.get("correct")
    )
    total   = len(questions)
    pct     = round((correct / total) * 100, 1) if total else 0
    raw     = f"{correct}/{total}"

    db = get_db_direct()
    try:
        # Quiz result record
        qr = models.QuizResult(
            learner_id   = user.id,
            subject      = subject,
            topic        = topic,
            class_level  = user.class_level,
            difficulty   = difficulty,
            num_questions= total,
            score_pct    = pct,
            score_raw    = raw,
            mode         = mode,
        )
        db.add(qr)

        # Update learner points + streak
        u = db.query(models.User).filter_by(id=user.id).first()
        if u:
            u.points      = (u.points or 0) + (correct * 5)
            u.quizzes_done= (u.quizzes_done or 0) + 1

        # Leaderboard entry — scoped to the learner's org
        lb = models.LeaderboardEntry(
            learner_name = user.name,
            learner_id   = user.id,
            org_id       = user.org_id,
            score_pct    = pct,
            score_raw    = raw,
            subject      = subject,
            class_level  = user.class_level,
            entry_type   = "quiz",
        )
        db.add(lb)
        db.commit()

        return jsonify({
            "score_pct":  pct,
            "score_raw":  raw,
            "correct":    correct,
            "total":      total,
            "grade":      _grade(pct),
            "points_earned": correct * 5,
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/learner/quiz/history", methods=["GET"])
@require_learner
@require_active_subscription
def quiz_history(user):
    db = get_db_direct()
    try:
        results = (
            db.query(models.QuizResult)
            .filter_by(learner_id=user.id)
            .order_by(models.QuizResult.created_at.desc())
            .limit(30)
            .all()
        )
        return jsonify({"results": [
            {
                "id":          r.id,
                "subject":     r.subject,
                "topic":       r.topic,
                "score_pct":   r.score_pct,
                "score_raw":   r.score_raw,
                "num_questions":r.num_questions,
                "difficulty":  r.difficulty,
                "mode":        r.mode,
                "created_at":  r.created_at.isoformat() if r.created_at else None,
            }
            for r in results
        ]})
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# ── AI TUTOR ──────────────────────────────────────────────────────────────
# POST /eduai/learner/tutor/chat
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/learner/tutor/chat", methods=["POST"])
@require_learner
@require_active_subscription
def tutor_chat_route(user):
    data     = request.get_json(force=True)
    message  = data.get("message", "").strip()
    history  = data.get("history", [])   # [{role, content}]
    language = data.get("language_name", user.language_name or "English")

    if not message:
        return jsonify({"error": "message is required"}), 400

    reply = ai_service.tutor_chat(
        message=message, history=history,
        class_level=user.class_level or "primary",
        sub_class=user.sub_class or "",
        language=language,
    )
    if not reply:
        return jsonify({"error": "Tutor unavailable — check API keys"}), 500

    return jsonify({"reply": reply})


# ════════════════════════════════════════════════════════════════════════════
# ── LEADERBOARD ───────────────────────────────────────────────────────────
# GET /eduai/leaderboard               → global
# GET /eduai/leaderboard?period=weekly → filtered
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/leaderboard", methods=["GET"])
def leaderboard():
    period      = request.args.get("period", "alltime")   # weekly | monthly | alltime
    class_level = request.args.get("class_level")
    subject     = request.args.get("subject")
    limit       = min(int(request.args.get("limit", 50)), 200)

    db = get_db_direct()
    try:
        # Try to get org_id from token so leaderboard is school-scoped
        org_id = None
        try:
            token = _get_token()
            if token:
                td = auth_service.verify_token(token)
                if td:
                    requesting_user = db.query(models.User).filter_by(id=td["user_id"]).first()
                    if requesting_user:
                        org_id = requesting_user.org_id
        except Exception:
            pass

        q = db.query(models.LeaderboardEntry)

        # Always filter by org when the user belongs to one
        if org_id:
            q = q.filter(models.LeaderboardEntry.org_id == org_id)

        if class_level:
            q = q.filter_by(class_level=class_level)
        if subject:
            q = q.filter(models.LeaderboardEntry.subject.ilike(f"%{subject}%"))
        if period == "weekly":
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=7)
            q = q.filter(models.LeaderboardEntry.created_at >= cutoff)
        elif period == "monthly":
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=30)
            q = q.filter(models.LeaderboardEntry.created_at >= cutoff)

        entries = q.order_by(models.LeaderboardEntry.score_pct.desc()).limit(limit).all()

        return jsonify({"entries": [
            {
                "rank":         i + 1,
                "name":         e.learner_name,
                "score_pct":    e.score_pct,
                "score_raw":    e.score_raw,
                "subject":      e.subject,
                "class_level":  e.class_level,
                "entry_type":   e.entry_type,
                "date":         e.created_at.strftime("%Y-%m-%d") if e.created_at else None,
            }
            for i, e in enumerate(entries)
        ]})
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# ── GAMES ─────────────────────────────────────────────────────────────────
# POST /eduai/games/save-score
# GET  /eduai/games/history
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/games/save-score", methods=["POST"])
@require_learner
@require_active_subscription
def games_save_score(user):
    data = request.get_json(force=True)
    db   = get_db_direct()
    try:
        session = models.GameSession(
            learner_id  = user.id,
            game_id     = data.get("game_id", "unknown"),
            game_title  = data.get("game_title", ""),
            score       = int(data.get("score", 0)),
            class_level = user.class_level,
        )
        db.add(session)

        # Award points proportional to game score
        u = db.query(models.User).filter_by(id=user.id).first()
        if u:
            earned   = min(int(data.get("score", 0) // 10), 50)
            u.points = (u.points or 0) + earned

        # Leaderboard entry for high scores — scoped to learner's org
        score_val = int(data.get("score", 0))
        if score_val >= 50:
            lb = models.LeaderboardEntry(
                learner_name = user.name,
                learner_id   = user.id,
                org_id       = user.org_id,
                score_pct    = min(score_val, 100),
                score_raw    = str(score_val),
                subject      = f"Game: {data.get('game_title','')}",
                class_level  = user.class_level,
                entry_type   = "quiz",
            )
            db.add(lb)

        db.commit()
        return jsonify({"status": "saved", "score": score_val})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/games/history", methods=["GET"])
@require_learner
@require_active_subscription
def games_history(user):
    db = get_db_direct()
    try:
        sessions = (
            db.query(models.GameSession)
            .filter_by(learner_id=user.id)
            .order_by(models.GameSession.played_at.desc())
            .limit(20)
            .all()
        )
        return jsonify({"sessions": [
            {
                "game_id":   s.game_id,
                "game_title":s.game_title,
                "score":     s.score,
                "played_at": s.played_at.isoformat() if s.played_at else None,
            }
            for s in sessions
        ]})
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# ── LEARNER DASHBOARD SUMMARY ────────────────────────────────────────────
# GET /eduai/learner/dashboard
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/learner/dashboard", methods=["GET"])
@require_learner
@require_active_subscription
def learner_dashboard(user):
    db = get_db_direct()
    try:
        quiz_results = (
            db.query(models.QuizResult)
            .filter_by(learner_id=user.id)
            .order_by(models.QuizResult.created_at.desc())
            .limit(10)
            .all()
        )
        game_sessions = (
            db.query(models.GameSession)
            .filter_by(learner_id=user.id)
            .order_by(models.GameSession.played_at.desc())
            .limit(5)
            .all()
        )
        # Rank on leaderboard — scoped to learner's org so schools don't mix
        rank_query = db.query(models.LeaderboardEntry)
        if user.org_id:
            rank_query = rank_query.filter(models.LeaderboardEntry.org_id == user.org_id)
        all_entries = rank_query.order_by(models.LeaderboardEntry.score_pct.desc()).all()
        rank = next(
            (i + 1 for i, e in enumerate(all_entries) if e.learner_id == user.id),
            None,
        )
        avg_score = None
        if quiz_results:
            avg_score = round(sum(q.score_pct for q in quiz_results) / len(quiz_results), 1)

        return jsonify({
            "user":        _user_dict(user),
            "rank":        rank,
            "avg_score":   avg_score,
            "quiz_count":  user.quizzes_done,
            "recent_quizzes": [
                {
                    "subject":  r.subject,
                    "topic":    r.topic,
                    "score":    r.score_pct,
                    "date":     r.created_at.strftime("%Y-%m-%d") if r.created_at else None,
                }
                for r in quiz_results
            ],
            "top_games": [
                {
                    "title": s.game_title,
                    "score": s.score,
                    "date":  s.played_at.strftime("%Y-%m-%d") if s.played_at else None,
                }
                for s in game_sessions
            ],
        })
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# ── HEALTH CHECK ──────────────────────────────────────────────────────────
# GET /eduai/health
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":   "online",
        "platform": "Learn-Beta",
        "version":  "1.0.0",
        "ai": {
            "anthropic": ai_service._ANTHROPIC_OK,
            "openai":    ai_service._OPENAI_OK,
        },
    })

# ════════════════════════════════════════════════════════════════════════════
# ── ADMIN PANEL ROUTES ────────────────────────────────────────────────────
# All protected with @require_admin (is_admin=True only)
# GET  /eduai/admin/overview
# POST /eduai/admin/regenerate-referral-code
# POST /eduai/admin/regenerate-admin-code
# GET  /eduai/admin/members
# POST /eduai/admin/members/<user_id>/remove
# POST /eduai/admin/members/<user_id>/promote
# POST /eduai/admin/members/<user_id>/demote
# GET  /eduai/admin/performance
# GET  /eduai/admin/leaderboard
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/admin/overview", methods=["GET"])
@require_admin
def admin_overview(user):
    db = get_db_direct()
    try:
        org = db.query(models.Organization).filter_by(id=user.org_id).first()
        if not org:
            return jsonify({"error": "No organization found"}), 404
        total_teachers = db.query(models.User).filter_by(
            org_id=user.org_id, role=models.UserRole.teacher
        ).count()
        total_learners = db.query(models.User).filter_by(
            org_id=user.org_id, role=models.UserRole.learner
        ).count()
        return jsonify({
            "org_name":       org.name,
            "referral_code":  org.referral_code,
            "admin_code":     org.admin_code,
            "total_teachers": total_teachers,
            "total_learners": total_learners,
            "created_at":     org.created_at.isoformat() if org.created_at else None,
        })
    finally:
        db.close()


@blueprint.route("/admin/regenerate-referral-code", methods=["POST"])
@require_admin
def admin_regen_referral(user):
    db = get_db_direct()
    try:
        org = db.query(models.Organization).filter_by(id=user.org_id).first()
        if not org:
            return jsonify({"error": "No organization found"}), 404
        org.referral_code = _gen_code("SCH")
        db.commit()
        return jsonify({"referral_code": org.referral_code})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/admin/regenerate-admin-code", methods=["POST"])
@require_admin
def admin_regen_admin_code(user):
    data = request.get_json(force=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "confirmation_required"}), 400
    db = get_db_direct()
    try:
        org = db.query(models.Organization).filter_by(id=user.org_id).first()
        if not org:
            return jsonify({"error": "No organization found"}), 404
        org.admin_code = _gen_code("ADM")
        db.commit()
        return jsonify({"admin_code": org.admin_code})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/admin/members", methods=["GET"])
@require_admin
def admin_members(user):
    db = get_db_direct()
    try:
        members = db.query(models.User).filter_by(org_id=user.org_id).all()
        return jsonify({"members": [
            {
                "id":          m.id,
                "name":        m.name,
                "email":       m.email,
                "role":        m.role.value,
                "is_admin":    bool(m.is_admin),
                "joined_at":   m.created_at.isoformat() if m.created_at else None,
                "last_active": m.last_active.isoformat() if m.last_active else None,
            }
            for m in members
        ]})
    finally:
        db.close()


@blueprint.route("/admin/members/<int:target_id>/remove", methods=["POST"])
@require_admin
def admin_remove_member(user, target_id):
    if target_id == user.id:
        return jsonify({"error": "Cannot remove yourself"}), 400
    db = get_db_direct()
    try:
        target = db.query(models.User).filter_by(id=target_id, org_id=user.org_id).first()
        if not target:
            return jsonify({"error": "Member not found in your organization"}), 400
        target.org_id = None
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/admin/members/<int:target_id>/promote", methods=["POST"])
@require_admin
def admin_promote_member(user, target_id):
    db = get_db_direct()
    try:
        target = db.query(models.User).filter_by(id=target_id, org_id=user.org_id).first()
        if not target:
            return jsonify({"error": "Member not found in your organization"}), 400
        if target.role != models.UserRole.teacher:
            return jsonify({"error": "Only teachers can be promoted to admin"}), 400
        target.is_admin = True
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/admin/members/<int:target_id>/demote", methods=["POST"])
@require_admin
def admin_demote_member(user, target_id):
    if target_id == user.id:
        # Check if this admin is the last one
        db = get_db_direct()
        try:
            other_admins = db.query(models.User).filter(
                models.User.org_id == user.org_id,
                models.User.is_admin == True,
                models.User.id != user.id
            ).count()
            if other_admins == 0:
                return jsonify({"error": "last_admin_cannot_demote"}), 400
        finally:
            db.close()

    db = get_db_direct()
    try:
        target = db.query(models.User).filter_by(id=target_id, org_id=user.org_id).first()
        if not target:
            return jsonify({"error": "Member not found in your organization"}), 400
        target.is_admin = False
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/admin/performance", methods=["GET"])
@require_admin
def admin_performance(user):
    """Org-wide aggregation of learner quiz performance."""
    db = get_db_direct()
    try:
        # Get all learner IDs in org
        learner_ids = [
            u.id for u in db.query(models.User).filter_by(
                org_id=user.org_id, role=models.UserRole.learner
            ).all()
        ]
        if not learner_ids:
            return jsonify({
                "total_quizzes": 0, "avg_score": None,
                "by_subject": [], "top_learners": []
            })

        all_results = db.query(models.QuizResult).filter(
            models.QuizResult.learner_id.in_(learner_ids)
        ).all()

        total_quizzes = len(all_results)
        avg_score = round(
            sum(r.score_pct for r in all_results) / total_quizzes, 1
        ) if all_results else None

        # Group by subject
        subject_map: dict = {}
        for r in all_results:
            s = r.subject or "General"
            if s not in subject_map:
                subject_map[s] = {"subject": s, "count": 0, "total_pct": 0.0}
            subject_map[s]["count"] += 1
            subject_map[s]["total_pct"] += r.score_pct
        by_subject = [
            {
                "subject":   v["subject"],
                "count":     v["count"],
                "avg_score": round(v["total_pct"] / v["count"], 1),
            }
            for v in subject_map.values()
        ]

        # Top 10 learners by avg quiz score
        learner_scores: dict = {}
        for r in all_results:
            if r.learner_id not in learner_scores:
                learner_scores[r.learner_id] = {"scores": [], "name": ""}
            learner_scores[r.learner_id]["scores"].append(r.score_pct)
        
        # Fetch names
        learner_objs = {u.id: u.name for u in db.query(models.User).filter(
            models.User.id.in_(list(learner_scores.keys()))
        ).all()}
        
        top_learners = sorted([
            {
                "learner_id":  lid,
                "name":        learner_objs.get(lid, "Unknown"),
                "avg_score":   round(sum(v["scores"]) / len(v["scores"]), 1),
                "quiz_count":  len(v["scores"]),
            }
            for lid, v in learner_scores.items()
        ], key=lambda x: x["avg_score"], reverse=True)[:10]

        return jsonify({
            "total_quizzes": total_quizzes,
            "avg_score":     avg_score,
            "by_subject":    by_subject,
            "top_learners":  top_learners,
        })
    finally:
        db.close()


@blueprint.route("/admin/leaderboard", methods=["GET"])
@require_admin
def admin_leaderboard(user):
    """School-scoped leaderboard — same shape as /eduai/leaderboard but org-filtered."""
    period      = request.args.get("period", "alltime")
    class_level = request.args.get("class_level")
    subject     = request.args.get("subject")
    limit       = min(int(request.args.get("limit", 50)), 200)

    db = get_db_direct()
    try:
        # Get org learner IDs
        learner_ids = [
            u.id for u in db.query(models.User).filter_by(
                org_id=user.org_id, role=models.UserRole.learner
            ).all()
        ]
        q = db.query(models.LeaderboardEntry).filter(
            models.LeaderboardEntry.learner_id.in_(learner_ids)
        )
        if class_level:
            q = q.filter_by(class_level=class_level)
        if subject:
            q = q.filter(models.LeaderboardEntry.subject.ilike(f"%{subject}%"))
        if period == "weekly":
            from datetime import timedelta
            q = q.filter(models.LeaderboardEntry.created_at >= datetime.utcnow() - timedelta(days=7))
        elif period == "monthly":
            from datetime import timedelta
            q = q.filter(models.LeaderboardEntry.created_at >= datetime.utcnow() - timedelta(days=30))

        entries = q.order_by(models.LeaderboardEntry.score_pct.desc()).limit(limit).all()

        # Fetch org name for label
        org = db.query(models.Organization).filter_by(id=user.org_id).first()
        org_name = org.name if org else "Your School"

        return jsonify({
            "org_name": org_name,
            "entries": [
                {
                    "rank":        i + 1,
                    "name":        e.learner_name,
                    "score_pct":   e.score_pct,
                    "score_raw":   e.score_raw,
                    "subject":     e.subject,
                    "class_level": e.class_level,
                    "entry_type":  e.entry_type,
                    "date":        e.created_at.strftime("%Y-%m-%d") if e.created_at else None,
                }
                for i, e in enumerate(entries)
            ]
        })
    finally:
        db.close()

# ════════════════════════════════════════════════════════════════════════════
# REGISTRATION FUNCTION — called from api.py
# ════════════════════════════════════════════════════════════════════════════

def register_eduai_routes(app):
    """
    Call this inside api.py to attach all EduAI routes to the OMEGA Flask app.

    Example (add to api.py after the existing optional route registrations):

        try:
            from eduai.app.routes.eduai_routes import register_eduai_routes
            register_eduai_routes(app)
            print("[API] ✅ EduAI routes registered at /eduai")
        except Exception as e:
            print(f"[API] EduAI routes skipped: {e}")
    """
    app.register_blueprint(blueprint)
    print("[EDUAI] ✅ All routes registered under /eduai")
