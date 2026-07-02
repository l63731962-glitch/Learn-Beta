"""
eduai/app/routes/phase1_routes.py
─────────────────────────────────────────────────────────────────────────────
Phase 1 feature routes — Daily Challenge, Streaks, Study Timer (Pomodoro +
coins), Parent Dashboard.

Registered onto the existing OMEGA Flask app in api.py via:

    from eduai.app.routes.phase1_routes import register_phase1_routes
    register_phase1_routes(app)

URL prefix: /eduai/phase1
Full routes e.g. GET /eduai/phase1/daily-challenge

This file follows the exact auth pattern used in eduai_routes.py / community_routes.py
(token in Authorization: Bearer header, looked up via auth_service.verify_token).
"""

import json
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify

from eduai.app.database import get_db_direct
from eduai.app import models
from eduai.app.streak_models import (
    DailyChallenge, StreakLog, StudySession, ParentLink, LearnerLinkCode,
)
from eduai.app.service import ai_service, auth_service

blueprint = Blueprint("eduai_phase1", __name__, url_prefix="/eduai/phase1")


# ════════════════════════════════════════════════════════════════════════════
# AUTH HELPERS (same pattern as eduai_routes.py)
# ════════════════════════════════════════════════════════════════════════════

def _get_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return (request.json or {}).get("token", "") if request.is_json else ""


def _current_user(db=None):
    close_db = db is None
    if close_db:
        db = get_db_direct()
    try:
        token = _get_token()
        if not token:
            return None, (jsonify({"error": "Unauthorized — no token"}), 401)
        td = auth_service.verify_token(token)
        if not td:
            return None, (jsonify({"error": "Token expired or invalid"}), 401)
        user = db.query(models.User).filter_by(id=td["user_id"]).first()
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


def require_active_subscription(f):
    """Stack directly under @require_auth / @require_learner / @require_parent.
    Blocks access once the 7-day trial has ended and no active PayPal
    subscription exists. Expects `user` as the first positional arg."""
    @wraps(f)
    def wrapper(user, *args, **kwargs):
        from datetime import datetime as _dt
        db = get_db_direct()
        try:
            fresh_user = db.query(models.User).filter_by(id=user.id).first()
            if not fresh_user:
                return jsonify({"error": "User not found"}), 404
            active = False
            if fresh_user.subscription_status == "active":
                active = True
            elif fresh_user.subscription_status == "trial":
                if fresh_user.trial_ends_at and _dt.utcnow() < fresh_user.trial_ends_at:
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


def require_learner(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, err = _current_user()
        if err:
            return err
        role = user.role.value if hasattr(user.role, "value") else user.role
        if role != "learner":
            return jsonify({"error": "Learner access required"}), 403
        return f(user, *args, **kwargs)
    return wrapper


def require_parent(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, err = _current_user()
        if err:
            return err
        role = user.role.value if hasattr(user.role, "value") else user.role
        if role != "parent":
            return jsonify({"error": "Parent access required"}), 403
        return f(user, *args, **kwargs)
    return wrapper


def _today_str() -> str:
    """UTC date string YYYY-MM-DD — used consistently for all streak/challenge keys."""
    return datetime.utcnow().strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")


# ════════════════════════════════════════════════════════════════════════════
# ── STREAK LOGIC (shared helper) ─────────────────────────────────────────────
# Call this any time a learner completes a qualifying activity
# (daily challenge, quiz, study session). Updates User.streak + User.points
# and inserts a StreakLog row (idempotent per day per source-agnostic streak).
# ════════════════════════════════════════════════════════════════════════════

def _record_activity_and_update_streak(db, user, source: str):
    """
    Returns dict: {streak, badge, badge_awarded, coins_awarded}
    badge is one of None | 'bronze' | 'silver' | 'gold'
    """
    today = _today_str()

    # Idempotency: don't double-count same day
    already_today = db.query(StreakLog).filter_by(
        user_id=user.id, activity_date=today
    ).first()

    if not already_today:
        db.add(StreakLog(user_id=user.id, activity_date=today, source=source))

        # Determine new streak value
        if user.last_activity_date == _yesterday_str():
            user.streak = (user.streak or 0) + 1
        elif user.last_activity_date == today:
            pass  # shouldn't happen given already_today check, but safe
        else:
            user.streak = 1  # streak broken or first ever activity

        user.last_activity_date = today

    # Badge thresholds — award once. We piggy-back on `points` bumps as the
    # "free pro month" / badge signal is left to the frontend to display
    # based on `streak` value returned here (7/30/100).
    badge = None
    if user.streak == 7:
        badge = "bronze"
    elif user.streak == 30:
        badge = "silver"
    elif user.streak == 100:
        badge = "gold"

    return {
        "streak": user.streak or 0,
        "badge": badge,
    }


# ════════════════════════════════════════════════════════════════════════════
# ── DAILY CHALLENGE ───────────────────────────────────────────────────────────
# GET  /eduai/phase1/daily-challenge          → today's challenge (creates if absent)
# POST /eduai/phase1/daily-challenge/submit   → submit answers, updates streak
# GET  /eduai/phase1/streak                   → current streak + completion status
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/daily-challenge", methods=["GET"])
@require_learner
@require_active_subscription
def get_daily_challenge(user):
    """
    Returns today's challenge for this learner. If none exists yet for today,
    generates one via ai_service.generate_quiz (5 questions, mixed difficulty,
    based on the learner's class_level + a rotating subject).
    """
    db = get_db_direct()
    try:
        today = _today_str()
        existing = db.query(DailyChallenge).filter_by(
            user_id=user.id, challenge_date=today
        ).first()

        if existing:
            return jsonify(_challenge_dict(existing))

        # Pick a subject — rotate through a simple pool based on day-of-year
        # so it varies daily without needing extra config.
        subject_pool = ["Mathematics", "English Language", "Basic Science",
                         "Social Studies", "General Knowledge"]
        day_index = datetime.utcnow().timetuple().tm_yday
        subject = subject_pool[day_index % len(subject_pool)]

        questions = ai_service.generate_quiz(
            subject=subject,
            topic="",
            class_level=user.class_level or "primary",
            sub_class=user.sub_class or "",
            num_q=5,
            difficulty="Mixed",
            language=user.language_name or "English",
        )
        if questions is None:
            return jsonify({"error": "AI generation failed — check API keys"}), 500

        challenge = DailyChallenge(
            user_id=user.id,
            challenge_date=today,
            subject=subject,
            topic=f"Daily Challenge — {subject}",
            questions_json=json.dumps(questions),
        )
        db.add(challenge)
        db.commit()
        db.refresh(challenge)

        return jsonify(_challenge_dict(challenge))
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/daily-challenge/submit", methods=["POST"])
@require_learner
@require_active_subscription
def submit_daily_challenge(user):
    """
    Body: { answers: [int, ...] }  (0-based option indices, same as quiz/submit)
    """
    data = request.get_json(force=True) or {}
    answers = data.get("answers", [])

    db = get_db_direct()
    try:
        today = _today_str()
        challenge = db.query(DailyChallenge).filter_by(
            user_id=user.id, challenge_date=today
        ).first()
        if not challenge:
            return jsonify({"error": "No challenge found for today — call GET /daily-challenge first"}), 404
        if challenge.is_completed:
            return jsonify({"error": "Today's challenge already completed", **_challenge_dict(challenge)}), 409

        questions = json.loads(challenge.questions_json or "[]")
        correct = sum(
            1 for i, q in enumerate(questions)
            if i < len(answers) and answers[i] == q.get("correct")
        )
        total = len(questions)
        pct = round((correct / total) * 100, 1) if total else 0

        challenge.is_completed = True
        challenge.score_pct = pct
        challenge.completed_at = datetime.utcnow()

        # Award points (same scale as regular quizzes: 5 pts/correct + 10 bonus)
        points_earned = correct * 5 + 10
        user.points = (user.points or 0) + points_earned

        streak_info = _record_activity_and_update_streak(db, user, source="daily_challenge")

        db.commit()

        return jsonify({
            "score_pct": pct,
            "correct": correct,
            "total": total,
            "points_earned": points_earned,
            "streak": streak_info["streak"],
            "badge_unlocked": streak_info["badge"],
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/streak", methods=["GET"])
@require_learner
@require_active_subscription
def get_streak(user):
    db = get_db_direct()
    try:
        today = _today_str()
        challenge = db.query(DailyChallenge).filter_by(
            user_id=user.id, challenge_date=today
        ).first()
        return jsonify({
            "streak": user.streak or 0,
            "coins": user.coins or 0,
            "today_completed": bool(challenge and challenge.is_completed),
            "badges": _badges_for_streak(user.streak or 0),
        })
    finally:
        db.close()


def _badges_for_streak(streak: int) -> list:
    badges = []
    if streak >= 7:
        badges.append({"name": "Bronze", "threshold": 7, "icon": "🥉"})
    if streak >= 30:
        badges.append({"name": "Silver", "threshold": 30, "icon": "🥈"})
    if streak >= 100:
        badges.append({"name": "Gold + Free Pro Month", "threshold": 100, "icon": "🥇"})
    return badges


def _challenge_dict(c: DailyChallenge) -> dict:
    return {
        "id": c.id,
        "date": c.challenge_date,
        "subject": c.subject,
        "topic": c.topic,
        "questions": json.loads(c.questions_json or "[]"),
        "is_completed": c.is_completed,
        "score_pct": c.score_pct,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# ── STUDY TIMER (POMODORO + COINS) ──────────────────────────────────────────
# POST /eduai/phase1/study/start     → begin a session
# POST /eduai/phase1/study/complete  → mark complete, award coins, update streak
# GET  /eduai/phase1/study/history   → recent sessions + total hours
# ════════════════════════════════════════════════════════════════════════════

# Coin reward: 1 coin per 5 minutes studied, capped at 12 coins (60 min)
def _coins_for_duration(minutes: int) -> int:
    return min(minutes // 5, 12)


@blueprint.route("/study/start", methods=["POST"])
@require_learner
@require_active_subscription
def study_start(user):
    """
    Body: { subject?: str, duration_min?: int }  (default 25 = standard Pomodoro)
    """
    data = request.get_json(force=True) or {}
    duration = int(data.get("duration_min", 25))
    duration = max(1, min(duration, 120))  # sanity clamp

    db = get_db_direct()
    try:
        session = StudySession(
            user_id=user.id,
            subject=data.get("subject", ""),
            duration_min=duration,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return jsonify({"session_id": session.id, "duration_min": duration})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/study/complete", methods=["POST"])
@require_learner
@require_active_subscription
def study_complete(user):
    """
    Body: { session_id: int }
    Marks the session complete, awards coins, and counts toward the daily streak.
    """
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")

    db = get_db_direct()
    try:
        session = db.query(StudySession).filter_by(
            id=session_id, user_id=user.id
        ).first()
        if not session:
            return jsonify({"error": "Session not found"}), 404
        if session.completed:
            return jsonify({"error": "Session already completed", "coins_earned": session.coins_earned}), 409

        coins = _coins_for_duration(session.duration_min)
        session.completed = True
        session.completed_at = datetime.utcnow()
        session.coins_earned = coins

        u = db.query(models.User).filter_by(id=user.id).first()
        u.coins = (u.coins or 0) + coins

        streak_info = _record_activity_and_update_streak(db, u, source="study_timer")

        db.commit()

        return jsonify({
            "coins_earned": coins,
            "total_coins": u.coins,
            "streak": streak_info["streak"],
            "badge_unlocked": streak_info["badge"],
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/study/history", methods=["GET"])
@require_learner
@require_active_subscription
def study_history(user):
    db = get_db_direct()
    try:
        sessions = (
            db.query(StudySession)
            .filter_by(user_id=user.id, completed=True)
            .order_by(StudySession.completed_at.desc())
            .limit(30)
            .all()
        )
        total_minutes = sum(s.duration_min for s in sessions)
        return jsonify({
            "total_hours": round(total_minutes / 60, 1),
            "total_sessions": len(sessions),
            "sessions": [
                {
                    "subject": s.subject,
                    "duration_min": s.duration_min,
                    "coins_earned": s.coins_earned,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                }
                for s in sessions
            ],
        })
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# ── PARENT DASHBOARD ─────────────────────────────────────────────────────────
# GET  /eduai/phase1/learner/link-code        → (learner) get/generate own link code
# POST /eduai/phase1/parent/link              → (parent) link to a learner via code
# GET  /eduai/phase1/parent/children          → (parent) list linked learners
# GET  /eduai/phase1/parent/child/<id>/report → (parent) weekly progress report
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/learner/link-code", methods=["GET"])
@require_learner
@require_active_subscription
def learner_link_code(user):
    """Returns the learner's persistent link code, generating one if needed."""
    db = get_db_direct()
    try:
        existing = db.query(LearnerLinkCode).filter_by(learner_id=user.id).first()
        if existing:
            return jsonify({"link_code": existing.link_code})

        # Generate a unique 8-char code
        for _ in range(10):
            code = "FAM-" + secrets.token_hex(4).upper()
            if not db.query(LearnerLinkCode).filter_by(link_code=code).first():
                break

        rec = LearnerLinkCode(learner_id=user.id, link_code=code)
        db.add(rec)
        db.commit()
        return jsonify({"link_code": code})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/parent/link", methods=["POST"])
@require_parent
@require_active_subscription
def parent_link_child(user):
    """
    Body: { link_code: str }
    Links the authenticated parent to the learner who owns this link_code.
    """
    data = request.get_json(force=True) or {}
    code = (data.get("link_code") or "").strip().upper()
    if not code:
        return jsonify({"error": "link_code is required"}), 400

    db = get_db_direct()
    try:
        rec = db.query(LearnerLinkCode).filter_by(link_code=code).first()
        if not rec:
            return jsonify({"error": "Invalid link code"}), 404

        # Avoid duplicate links
        existing = db.query(ParentLink).filter_by(
            parent_id=user.id, learner_id=rec.learner_id
        ).first()
        if existing:
            return jsonify({"error": "Already linked to this learner"}), 409

        link = ParentLink(parent_id=user.id, learner_id=rec.learner_id)
        db.add(link)
        db.commit()

        child = db.query(models.User).filter_by(id=rec.learner_id).first()
        return jsonify({"success": True, "child_name": child.name if child else None})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/parent/children", methods=["GET"])
@require_parent
@require_active_subscription
def parent_children(user):
    db = get_db_direct()
    try:
        links = db.query(ParentLink).filter_by(parent_id=user.id).all()
        children = []
        for link in links:
            child = db.query(models.User).filter_by(id=link.learner_id).first()
            if child:
                children.append({
                    "id": child.id,
                    "name": child.name,
                    "class_level": child.class_level,
                    "sub_class": child.sub_class,
                    "school": child.school,
                    "streak": child.streak or 0,
                    "points": child.points or 0,
                })
        return jsonify({"children": children})
    finally:
        db.close()


@blueprint.route("/parent/child/<int:child_id>/report", methods=["GET"])
@require_parent
@require_active_subscription
def parent_child_report(user, child_id):
    """
    Weekly progress report for a linked child.
    Returns recent quiz results, subject averages, and an AI-written summary
    if ai_service.generate_performance_insight is available.
    """
    db = get_db_direct()
    try:
        link = db.query(ParentLink).filter_by(
            parent_id=user.id, learner_id=child_id
        ).first()
        if not link:
            return jsonify({"error": "Not linked to this learner"}), 403

        child = db.query(models.User).filter_by(id=child_id).first()
        if not child:
            return jsonify({"error": "Learner not found"}), 404

        cutoff = datetime.utcnow() - timedelta(days=7)
        recent_quizzes = (
            db.query(models.QuizResult)
            .filter(models.QuizResult.learner_id == child_id,
                    models.QuizResult.created_at >= cutoff)
            .order_by(models.QuizResult.created_at.desc())
            .all()
        )

        # Subject averages
        subject_map: dict = {}
        for r in recent_quizzes:
            s = r.subject or "General"
            subject_map.setdefault(s, []).append(r.score_pct)
        by_subject = [
            {"subject": s, "avg_score": round(sum(v) / len(v), 1), "count": len(v)}
            for s, v in subject_map.items()
        ]
        weak_subjects = [s["subject"] for s in sorted(by_subject, key=lambda x: x["avg_score"])[:2]]

        avg_score = (
            round(sum(r.score_pct for r in recent_quizzes) / len(recent_quizzes), 1)
            if recent_quizzes else None
        )

        insight = None
        if recent_quizzes:
            insight = ai_service.generate_performance_insight(
                subject="this week's activity",
                avg_score=avg_score or 0,
                weak_topics=weak_subjects,
                num_students=1,
                language=user.language_name or "English",
            )

        return jsonify({
            "child": {
                "id": child.id,
                "name": child.name,
                "class_level": child.class_level,
                "streak": child.streak or 0,
                "points": child.points or 0,
            },
            "week_summary": {
                "quizzes_taken": len(recent_quizzes),
                "avg_score": avg_score,
                "by_subject": by_subject,
                "weak_subjects": weak_subjects,
            },
            "ai_insight": insight,
            "recent_quizzes": [
                {
                    "subject": r.subject,
                    "topic": r.topic,
                    "score_pct": r.score_pct,
                    "date": r.created_at.strftime("%Y-%m-%d") if r.created_at else None,
                }
                for r in recent_quizzes[:10]
            ],
        })
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# REGISTRATION
# ════════════════════════════════════════════════════════════════════════════

def register_phase1_routes(app):
    """
    Call this inside api.py, alongside the other EduAI registrations:

        try:
            from eduai.app.routes.phase1_routes import register_phase1_routes
            register_phase1_routes(app)
            print("[API] ✅ Phase1 routes registered at /eduai/phase1")
        except Exception as e:
            print(f"[API] Phase1 routes skipped: {e}")
    """
    app.register_blueprint(blueprint)
    print("[EDUAI-PHASE1] ✅ Daily Challenge / Streaks / Study Timer / Parent Dashboard registered at /eduai/phase1")
