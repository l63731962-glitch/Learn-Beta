"""
eduai/app/routes/phase3_routes.py
Place this file at:  eduai/app/routes/phase3_routes.py
(Same folder as eduai_routes.py, community_routes.py, phase1_routes.py, phase2_routes.py)

Phase 3 — Chunk A: Curriculum Gap Detector + Exam Readiness Score.

Registered at /eduai/phase3/* via register_phase3_routes(app), called from api.py
the same way Phase1/Phase2 are.
"""

import json
import re
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify

from eduai.app.database import get_db_direct
from eduai.app import models
from eduai.app import phase3_models
from eduai.app.service import ai_service, auth_service

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import CouldNotRetrieveTranscript
    _YT_OK = True
except Exception:
    _YT_OK = False

blueprint = Blueprint("phase3", __name__, url_prefix="/eduai/phase3")


# ════════════════════════════════════════════════════════════════════════════
# AUTH HELPER — mirrors eduai_routes.py's require_learner
# ════════════════════════════════════════════════════════════════════════════

def _get_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.json.get("token", "") if request.is_json else ""


def require_learner(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        db = get_db_direct()
        try:
            token = _get_token()
            if not token:
                return jsonify({"error": "Unauthorized — no token"}), 401
            td = auth_service.verify_token(token)
            if not td:
                return jsonify({"error": "Token expired or invalid"}), 401
            user = db.query(models.User).filter_by(id=td["user_id"]).first()
            if not user:
                return jsonify({"error": "User not found"}), 404
            if user.role != models.UserRole.learner:
                return jsonify({"error": "Learner access required"}), 403
            return f(user, *args, **kwargs)
        finally:
            db.close()
    return wrapper


def require_teacher(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        db = get_db_direct()
        try:
            token = _get_token()
            if not token:
                return jsonify({"error": "Unauthorized — no token"}), 401
            td = auth_service.verify_token(token)
            if not td:
                return jsonify({"error": "Token expired or invalid"}), 401
            user = db.query(models.User).filter_by(id=td["user_id"]).first()
            if not user:
                return jsonify({"error": "User not found"}), 404
            if user.role != models.UserRole.teacher:
                return jsonify({"error": "Teacher access required"}), 403
            return f(user, *args, **kwargs)
        finally:
            db.close()
    return wrapper


def require_active_subscription(f):
    """Stack directly under @require_learner / @require_teacher. Blocks
    access once the 7-day trial has ended and no active PayPal
    subscription exists."""
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


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _gather_history(db, learner_id: int):
    """Pull recent quiz history for a learner, in the shape
    ai_service.detect_curriculum_gaps / assess_exam_readiness expect.

    Returns (quiz_history, []) — the second element is a placeholder; CBT
    test history is fetched separately via _test_history_for_learner(db, user)
    since TestResult requires a name-based lookup (see that function's docstring).
    """
    quiz_results = (
        db.query(models.QuizResult)
        .filter_by(learner_id=learner_id)
        .order_by(models.QuizResult.created_at.desc())
        .limit(50)
        .all()
    )
    quiz_history = [
        {
            "subject": q.subject or "General",
            "topic": q.topic or "General",
            "score_pct": q.score_pct,
            "difficulty": q.difficulty or "Medium",
        }
        for q in quiz_results
    ]
    return quiz_history, []


# ════════════════════════════════════════════════════════════════════════════
# CURRICULUM GAP DETECTOR
# GET  /eduai/phase3/curriculum-gaps         → latest cached report (or generate)
# POST /eduai/phase3/curriculum-gaps/refresh → force regenerate
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/curriculum-gaps", methods=["GET"])
@require_learner
@require_active_subscription
def curriculum_gaps(user):
    db = get_db_direct()
    try:
        today = _today()
        existing = (
            db.query(phase3_models.CurriculumGapReport)
            .filter_by(learner_id=user.id, report_date=today)
            .first()
        )
        if existing:
            return jsonify(_gap_report_dict(existing))
        return _generate_gap_report(db, user)
    finally:
        db.close()


@blueprint.route("/curriculum-gaps/refresh", methods=["POST"])
@require_learner
@require_active_subscription
def curriculum_gaps_refresh(user):
    db = get_db_direct()
    try:
        return _generate_gap_report(db, user)
    finally:
        db.close()


def _generate_gap_report(db, user):
    quiz_history, _ = _gather_history(db, user.id)
    test_history = _test_history_for_learner(db, user)

    result = ai_service.detect_curriculum_gaps(
        learner_name=user.name,
        class_level=user.class_level or "primary",
        sub_class=user.sub_class or "",
        quiz_history=quiz_history,
        test_history=test_history,
        language=user.language_name or "English",
    )
    if result is None:
        return jsonify({"error": "AI analysis failed — check API keys"}), 500

    report = phase3_models.CurriculumGapReport(
        learner_id=user.id,
        report_date=_today(),
        gaps_json=json.dumps(result.get("gaps", [])),
        summary_text=result.get("summary", ""),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return jsonify(_gap_report_dict(report))


def _gap_report_dict(r) -> dict:
    try:
        gaps = json.loads(r.gaps_json)
    except Exception:
        gaps = []
    return {
        "report_date": r.report_date,
        "gaps": gaps,
        "summary": r.summary_text,
        "generated_at": r.created_at.isoformat() if r.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# EXAM READINESS SCORE
# POST /eduai/phase3/exam-readiness          → {exam_type, subject} → score
# GET  /eduai/phase3/exam-readiness/history  → past scores for this learner
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/exam-readiness", methods=["POST"])
@require_learner
@require_active_subscription
def exam_readiness(user):
    data      = request.get_json(force=True) or {}
    exam_type = (data.get("exam_type") or "").strip()
    subject   = (data.get("subject") or "").strip()

    if not exam_type or not subject:
        return jsonify({"error": "exam_type and subject are required"}), 400

    db = get_db_direct()
    try:
        quiz_history, _ = _gather_history(db, user.id)
        test_history    = _test_history_for_learner(db, user)

        result = ai_service.assess_exam_readiness(
            learner_name=user.name,
            exam_type=exam_type,
            subject=subject,
            class_level=user.class_level or "primary",
            sub_class=user.sub_class or "",
            quiz_history=quiz_history,
            test_history=test_history,
            language=user.language_name or "English",
        )
        if result is None:
            return jsonify({"error": "AI analysis failed — check API keys"}), 500

        score = phase3_models.ExamReadinessScore(
            learner_id=user.id,
            exam_type=exam_type,
            subject=subject,
            readiness_pct=result.get("readiness_pct", 0.0),
            breakdown_json=json.dumps(result.get("breakdown", {})),
            recommendation=result.get("recommendation", ""),
        )
        db.add(score)
        db.commit()
        db.refresh(score)

        return jsonify(_readiness_dict(score))
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/exam-readiness/history", methods=["GET"])
@require_learner
@require_active_subscription
def exam_readiness_history(user):
    db = get_db_direct()
    try:
        scores = (
            db.query(phase3_models.ExamReadinessScore)
            .filter_by(learner_id=user.id)
            .order_by(phase3_models.ExamReadinessScore.created_at.desc())
            .limit(20)
            .all()
        )
        return jsonify({"scores": [_readiness_dict(s) for s in scores]})
    finally:
        db.close()


def _readiness_dict(s) -> dict:
    try:
        breakdown = json.loads(s.breakdown_json) if s.breakdown_json else {}
    except Exception:
        breakdown = {}
    return {
        "id":             s.id,
        "exam_type":      s.exam_type,
        "subject":        s.subject,
        "readiness_pct":  s.readiness_pct,
        "breakdown":      breakdown,
        "recommendation": s.recommendation,
        "created_at":     s.created_at.isoformat() if s.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# HELPER — best-effort CBT test history lookup by student name
# ════════════════════════════════════════════════════════════════════════════

def _test_history_for_learner(db, user) -> list:
    """
    TestResult rows are submitted anonymously by students (no learner_id FK —
    see CAVEAT in the integration notes). As a best-effort signal, match by
    exact (case-insensitive) name against the logged-in learner's name.
    If no name match is found, returns an empty list — the AI functions
    handle empty test_history gracefully.
    """
    if not user.name:
        return []
    rows = (
        db.query(models.TestResult, models.TestSession)
        .join(models.TestSession, models.TestResult.session_id == models.TestSession.id)
        .filter(models.TestResult.student_name.ilike(user.name.strip()))
        .order_by(models.TestResult.submitted_at.desc())
        .limit(30)
        .all()
    )
    return [
        {
            "subject": s.subject or "General",
            "class_level": s.class_level or "",
            "score_pct": r.score_pct,
        }
        for r, s in rows
    ]


# ════════════════════════════════════════════════════════════════════════════
# REPORT CARD GENERATOR
# POST /eduai/phase3/report-card           → generate for self (learner)
# GET  /eduai/phase3/report-card/history    → past report cards for self
# GET  /eduai/phase3/report-card/<id>       → fetch one report card (full content)
# POST /eduai/phase3/teacher/report-card    → teacher generates for a student
# ════════════════════════════════════════════════════════════════════════════

def _resolve_period(data: dict) -> tuple:
    """Returns (period_start, period_end, days_back) as ('YYYY-MM-DD', 'YYYY-MM-DD', int)."""
    days_back = int(data.get("days_back", 30))
    days_back = max(1, min(days_back, 365))
    end   = datetime.utcnow()
    start = end - timedelta(days=days_back)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), days_back


def _quiz_history_for_report(db, learner_id: int, days_back: int) -> list:
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    results = (
        db.query(models.QuizResult)
        .filter(models.QuizResult.learner_id == learner_id)
        .filter(models.QuizResult.created_at >= cutoff)
        .order_by(models.QuizResult.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "subject": q.subject or "General",
            "topic": q.topic or "General",
            "score_pct": q.score_pct,
            "difficulty": q.difficulty or "Medium",
            "date": q.created_at.strftime("%Y-%m-%d") if q.created_at else "",
        }
        for q in results
    ]


def _game_history_for_report(db, learner_id: int, days_back: int) -> list:
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    sessions = (
        db.query(models.GameSession)
        .filter(models.GameSession.learner_id == learner_id)
        .filter(models.GameSession.played_at >= cutoff)
        .order_by(models.GameSession.played_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "title": s.game_title or s.game_id,
            "score": s.score,
            "date": s.played_at.strftime("%Y-%m-%d") if s.played_at else "",
        }
        for s in sessions
    ]


def _generate_report_card_for(db, learner, generated_by_id=None):
    quiz_history  = _quiz_history_for_report(db, learner.id, 30)
    game_history  = _game_history_for_report(db, learner.id, 30)
    period_start, period_end, _ = _resolve_period({"days_back": 30})

    result = ai_service.generate_report_card(
        learner_name=learner.name,
        class_level=learner.class_level or "primary",
        sub_class=learner.sub_class or "",
        period_start=period_start,
        period_end=period_end,
        quiz_history=quiz_history,
        game_history=game_history,
        streak_days=learner.streak or 0,
        language=learner.language_name or "English",
    )
    if result is None:
        return None

    card = phase3_models.ReportCard(
        learner_id=learner.id,
        generated_by_id=generated_by_id,
        period_start=period_start,
        period_end=period_end,
        overall_grade=result.get("overall_grade", "C"),
        content_md=result.get("content_md", ""),
        stats_json=json.dumps(result.get("stats", {})),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


@blueprint.route("/report-card", methods=["POST"])
@require_learner
@require_active_subscription
def report_card_generate(user):
    db = get_db_direct()
    try:
        card = _generate_report_card_for(db, user, generated_by_id=None)
        if card is None:
            return jsonify({"error": "AI generation failed — check API keys"}), 500
        return jsonify(_report_card_dict(card))
    finally:
        db.close()


@blueprint.route("/report-card/history", methods=["GET"])
@require_learner
@require_active_subscription
def report_card_history(user):
    db = get_db_direct()
    try:
        cards = (
            db.query(phase3_models.ReportCard)
            .filter_by(learner_id=user.id)
            .order_by(phase3_models.ReportCard.created_at.desc())
            .limit(20)
            .all()
        )
        # History list omits full content_md to keep payload small
        return jsonify({"reports": [_report_card_summary_dict(c) for c in cards]})
    finally:
        db.close()


@blueprint.route("/report-card/<int:report_id>", methods=["GET"])
@require_learner
@require_active_subscription
def report_card_detail(user, report_id):
    db = get_db_direct()
    try:
        card = (
            db.query(phase3_models.ReportCard)
            .filter_by(id=report_id, learner_id=user.id)
            .first()
        )
        if not card:
            return jsonify({"error": "Not found"}), 404
        return jsonify(_report_card_dict(card))
    finally:
        db.close()


@blueprint.route("/teacher/report-card", methods=["POST"])
@require_teacher
@require_active_subscription
def teacher_report_card_generate(user):
    """Teacher generates a report card for one of their students.
    Body: { learner_id: int }
    NOTE: any teacher can generate a report card for any learner — there is
    no class-roster check here (the data model has no teacher<->learner
    enrollment link). See caveats."""
    data = request.get_json(force=True) or {}
    learner_id = data.get("learner_id")
    if not learner_id:
        return jsonify({"error": "learner_id is required"}), 400

    db = get_db_direct()
    try:
        learner = db.query(models.User).filter_by(id=learner_id, role=models.UserRole.learner).first()
        if not learner:
            return jsonify({"error": "Learner not found"}), 404

        card = _generate_report_card_for(db, learner, generated_by_id=user.id)
        if card is None:
            return jsonify({"error": "AI generation failed — check API keys"}), 500
        return jsonify(_report_card_dict(card))
    finally:
        db.close()


def _report_card_dict(c) -> dict:
    try:
        stats = json.loads(c.stats_json) if c.stats_json else {}
    except Exception:
        stats = {}
    return {
        "id":             c.id,
        "period_start":   c.period_start,
        "period_end":     c.period_end,
        "overall_grade":  c.overall_grade,
        "content_md":     c.content_md,
        "stats":          stats,
        "created_at":     c.created_at.isoformat() if c.created_at else None,
    }


def _report_card_summary_dict(c) -> dict:
    try:
        stats = json.loads(c.stats_json) if c.stats_json else {}
    except Exception:
        stats = {}
    return {
        "id":            c.id,
        "period_start":  c.period_start,
        "period_end":    c.period_end,
        "overall_grade": c.overall_grade,
        "avg_score":     stats.get("avg_score"),
        "created_at":    c.created_at.isoformat() if c.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# ESSAY / ASSIGNMENT CHECKER
# POST /eduai/phase3/essay-check         → submit essay, get AI feedback
# GET  /eduai/phase3/essay-check/history → past checks for this learner
# GET  /eduai/phase3/essay-check/<id>    → fetch one check (full text + feedback)
# ════════════════════════════════════════════════════════════════════════════

@blueprint.route("/essay-check", methods=["POST"])
@require_learner
@require_active_subscription
def essay_check(user):
    data       = request.get_json(force=True) or {}
    essay_text = (data.get("essay_text") or "").strip()
    subject    = (data.get("subject") or "General").strip()
    title      = (data.get("title") or "").strip()

    if not essay_text:
        return jsonify({"error": "essay_text is required"}), 400
    if len(essay_text) < 20:
        return jsonify({"error": "essay_text is too short to evaluate (minimum ~20 characters)"}), 400

    result = ai_service.check_essay(
        essay_text=essay_text,
        subject=subject,
        class_level=user.class_level or "primary",
        sub_class=user.sub_class or "",
        language=user.language_name or "English",
    )
    if result is None:
        return jsonify({"error": "AI generation failed — check API keys"}), 500

    db = get_db_direct()
    try:
        check = phase3_models.EssayCheck(
            learner_id=user.id,
            subject=subject,
            title=title or None,
            essay_text=essay_text,
            feedback_json=json.dumps(result),
        )
        db.add(check)
        db.commit()
        db.refresh(check)
        return jsonify(_essay_check_dict(check))
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/essay-check/history", methods=["GET"])
@require_learner
@require_active_subscription
def essay_check_history(user):
    db = get_db_direct()
    try:
        checks = (
            db.query(phase3_models.EssayCheck)
            .filter_by(learner_id=user.id)
            .order_by(phase3_models.EssayCheck.created_at.desc())
            .limit(20)
            .all()
        )
        return jsonify({"checks": [_essay_check_summary_dict(c) for c in checks]})
    finally:
        db.close()


@blueprint.route("/essay-check/<int:check_id>", methods=["GET"])
@require_learner
@require_active_subscription
def essay_check_detail(user, check_id):
    db = get_db_direct()
    try:
        check = (
            db.query(phase3_models.EssayCheck)
            .filter_by(id=check_id, learner_id=user.id)
            .first()
        )
        if not check:
            return jsonify({"error": "Not found"}), 404
        return jsonify(_essay_check_dict(check))
    finally:
        db.close()


def _essay_check_dict(c) -> dict:
    try:
        feedback = json.loads(c.feedback_json)
    except Exception:
        feedback = {}
    return {
        "id":         c.id,
        "subject":    c.subject,
        "title":      c.title,
        "essay_text": c.essay_text,
        "feedback":   feedback,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _essay_check_summary_dict(c) -> dict:
    try:
        feedback = json.loads(c.feedback_json)
    except Exception:
        feedback = {}
    return {
        "id":            c.id,
        "subject":       c.subject,
        "title":         c.title,
        "overall_score": feedback.get("overall_score"),
        "summary":       feedback.get("summary"),
        "created_at":    c.created_at.isoformat() if c.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# LESSON PLAN FROM YOUTUBE
# POST /eduai/phase3/video-lesson         → {video_url, subject, class_level?, sub_class?}
# GET  /eduai/phase3/video-lesson/history → past video lesson plans for this teacher
# GET  /eduai/phase3/video-lesson/<id>    → full lesson plan
# ════════════════════════════════════════════════════════════════════════════

_YT_ID_PATTERNS = [
    r"(?:youtube\.com/watch\?v=|youtube\.com/embed/|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
]


def _extract_video_id(url: str) -> str:
    url = (url or "").strip()
    for pat in _YT_ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    # Fallback: if the user pasted a bare 11-char ID
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    return ""


def _short_err(e: Exception) -> str:
    """youtube-transcript-api exceptions often contain multi-line, verbose
    messages (including links to file GitHub issues). Collapse to a single
    line and cap length for clean JSON/UI display."""
    msg = str(e).strip()
    # Collapse all whitespace/newlines to single spaces
    msg = re.sub(r"\s+", " ", msg)
    # Drop the boilerplate "create an issue" suffix if present
    msg = re.sub(r"\s*If you are sure.*$", "", msg)
    if len(msg) > 300:
        msg = msg[:300].rstrip() + "..."
    return msg or e.__class__.__name__


def _fetch_transcript(video_id: str):
    """
    Returns (transcript_text, lang_code, error_message).
    On success, error_message is None. On failure, transcript_text is None
    and error_message explains why.

    Uses youtube-transcript-api v1.x: YouTubeTranscriptApi().list(video_id)
    returns a TranscriptList; .fetch() on a Transcript returns a
    FetchedTranscript with a .snippets list of objects (each has .text).
    """
    if not _YT_OK:
        return None, None, (
            "youtube-transcript-api is not installed. Run: "
            "pip install youtube-transcript-api"
        )

    api = YouTubeTranscriptApi()

    try:
        transcript_list = api.list(video_id)
    except CouldNotRetrieveTranscript as e:
        # Covers TranscriptsDisabled, VideoUnavailable, NoTranscriptFound at
        # the list() stage, IpBlocked/RequestBlocked/YouTubeRequestFailed
        # (YouTube blocking requests from this server's IP — common on cloud
        # hosts), AgeRestricted, InvalidVideoId, etc.
        return None, None, f"Could not retrieve transcript: {_short_err(e)}"
    except Exception as e:
        return None, None, f"Could not fetch transcript info: {_short_err(e)}"

    # Prefer a manually-created transcript, then any auto-generated one,
    # trying English first, then any available language.
    transcript = None
    lang_code  = None
    try:
        transcript = transcript_list.find_transcript(["en"])
        lang_code  = transcript.language_code
    except CouldNotRetrieveTranscript:
        # No English transcript — fall back to iterating all available ones,
        # preferring manually-created over auto-generated.
        manual_fallback = None
        any_fallback    = None
        for t in transcript_list:
            if any_fallback is None:
                any_fallback = t
            if not t.is_generated and manual_fallback is None:
                manual_fallback = t
        transcript = manual_fallback or any_fallback
        if transcript:
            lang_code = transcript.language_code

    if transcript is None:
        return None, None, "No transcript available for this video in any language."

    try:
        fetched = transcript.fetch()
    except CouldNotRetrieveTranscript as e:
        return None, None, f"Failed to fetch transcript content: {_short_err(e)}"
    except Exception as e:
        return None, None, f"Failed to fetch transcript content: {_short_err(e)}"

    text = " ".join(snippet.text for snippet in fetched.snippets)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return None, None, "Transcript was empty."

    return text, lang_code, None


@blueprint.route("/video-lesson", methods=["POST"])
@require_teacher
@require_active_subscription
def video_lesson_generate(user):
    data        = request.get_json(force=True) or {}
    video_url   = (data.get("video_url") or "").strip()
    subject     = (data.get("subject") or "General").strip()
    class_level = data.get("class_level", user.class_level or "primary")
    sub_class   = data.get("sub_class", "")
    curriculum  = data.get("curriculum", "Nigerian (NERDC)")

    if not video_url:
        return jsonify({"error": "video_url is required"}), 400

    video_id = _extract_video_id(video_url)
    if not video_id:
        return jsonify({"error": "Could not extract a YouTube video ID from that URL"}), 400

    transcript_text, lang_code, err = _fetch_transcript(video_id)
    if err:
        if err.startswith("Could not retrieve transcript") or err.startswith("Could not fetch transcript info") or err.startswith("Failed to fetch transcript content"):
            # Upstream YouTube issue: IP blocked, transcripts disabled, video
            # unavailable, age-restricted, etc. — not something the caller
            # can fix by retrying with different input.
            status = 502
        else:
            # "No transcript available...", "Transcript was empty.",
            # "youtube-transcript-api is not installed..."
            status = 404
        return jsonify({"error": err}), status

    content_md = ai_service.generate_lesson_from_transcript(
        transcript_text=transcript_text,
        subject=subject,
        class_level=class_level,
        sub_class=sub_class,
        curriculum=curriculum,
        language=user.language_name or "English",
    )
    if not content_md:
        return jsonify({"error": "AI generation failed — check API keys"}), 500

    db = get_db_direct()
    try:
        plan = phase3_models.VideoLessonPlan(
            teacher_id=user.id,
            video_url=video_url,
            video_id=video_id,
            video_title=None,  # see caveat — fetching title needs a separate API call
            subject=subject,
            class_level=class_level,
            transcript_lang=lang_code,
            content_md=content_md,
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return jsonify(_video_lesson_dict(plan))
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/video-lesson/history", methods=["GET"])
@require_teacher
@require_active_subscription
def video_lesson_history(user):
    db = get_db_direct()
    try:
        plans = (
            db.query(phase3_models.VideoLessonPlan)
            .filter_by(teacher_id=user.id)
            .order_by(phase3_models.VideoLessonPlan.created_at.desc())
            .limit(20)
            .all()
        )
        return jsonify({"plans": [_video_lesson_summary_dict(p) for p in plans]})
    finally:
        db.close()


@blueprint.route("/video-lesson/<int:plan_id>", methods=["GET"])
@require_teacher
@require_active_subscription
def video_lesson_detail(user, plan_id):
    db = get_db_direct()
    try:
        plan = (
            db.query(phase3_models.VideoLessonPlan)
            .filter_by(id=plan_id, teacher_id=user.id)
            .first()
        )
        if not plan:
            return jsonify({"error": "Not found"}), 404
        return jsonify(_video_lesson_dict(plan))
    finally:
        db.close()


def _video_lesson_dict(p) -> dict:
    return {
        "id":              p.id,
        "video_url":       p.video_url,
        "video_id":        p.video_id,
        "video_title":     p.video_title,
        "subject":         p.subject,
        "class_level":     p.class_level,
        "transcript_lang": p.transcript_lang,
        "content_md":      p.content_md,
        "created_at":      p.created_at.isoformat() if p.created_at else None,
    }


def _video_lesson_summary_dict(p) -> dict:
    return {
        "id":          p.id,
        "video_url":   p.video_url,
        "video_id":    p.video_id,
        "video_title": p.video_title,
        "subject":     p.subject,
        "class_level": p.class_level,
        "created_at":  p.created_at.isoformat() if p.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# REGISTRATION
# ════════════════════════════════════════════════════════════════════════════

def register_phase3_routes(app):
    app.register_blueprint(blueprint)
    print("[PHASE3] ✅ Routes registered at /eduai/phase3")
