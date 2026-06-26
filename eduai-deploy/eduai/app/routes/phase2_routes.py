"""
eduai/app/routes/phase2_routes.py
─────────────────────────────────────────────────────────────────────────────
Phase 2 feature routes — Battle Mode (1v1 Quiz Race, polling-based) and
Textbook Photo Scanner.

Registered onto the existing OMEGA Flask app in api.py via:

    from eduai.app.routes.phase2_routes import register_phase2_routes
    register_phase2_routes(app)

URL prefix: /eduai/phase2

BATTLE MODE — HOW THE CLIENT SHOULD USE THIS
─────────────────────────────────────────────
1. Player A: POST /eduai/phase2/battle/create  → {battle_id, invite_code}
   Player A shares invite_code (e.g. via chat/WhatsApp).
2. Player B: POST /eduai/phase2/battle/join  body:{invite_code}
   → battle moves to status='active', questions revealed to both.
3. BOTH players poll: GET /eduai/phase2/battle/<id>/state  every ~2s
   → returns both players' progress, scores, and status.
4. On each answer: POST /eduai/phase2/battle/<id>/answer
   body:{question_index, chosen_index}
5. When a player has answered all questions, the server checks if the OTHER
   player has too — if so, status→'finished' and winner_id is set (higher
   score wins; tie on score → faster total time wins; otherwise draw).
   If only one player finishes, status stays 'active' until the second
   player finishes too OR a client-side timer expires (handled client-side
   — there's no server-side cron in this stack).

This is polling, not WebSockets — by design, to avoid adding flask-socketio
as a new dependency. 2-second poll intervals feel "live enough" for a 5-10
question quiz race.
"""

import base64
import json
import secrets
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify

from eduai.app.database import get_db_direct
from eduai.app import models
from eduai.app.battle_models import Battle, BattleAnswer, TextbookScan
from eduai.app.service import ai_service, auth_service

blueprint = Blueprint("eduai_phase2", __name__, url_prefix="/eduai/phase2")


# ════════════════════════════════════════════════════════════════════════════
# AUTH HELPERS (same pattern as eduai_routes.py / phase1_routes.py)
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


# ════════════════════════════════════════════════════════════════════════════
# ── BATTLE MODE ───────────────────────────────────────────────────────────
# POST /eduai/phase2/battle/create        → start a battle, get invite code
# POST /eduai/phase2/battle/join          → join via invite code
# GET  /eduai/phase2/battle/<id>/state    → poll for live state
# POST /eduai/phase2/battle/<id>/answer   → submit an answer
# GET  /eduai/phase2/battle/my-active     → check for an in-progress battle
# ════════════════════════════════════════════════════════════════════════════

def _gen_invite_code() -> str:
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
    return "".join(secrets.choice(chars) for _ in range(6))


@blueprint.route("/battle/create", methods=["POST"])
@require_learner
def battle_create(user):
    """
    Body: { subject?: str, topic?: str, num_questions?: int (default 5, max 10),
            difficulty?: str (default 'Medium') }
    Creates a battle in 'waiting' status with a 6-char invite code.
    """
    data = request.get_json(force=True) or {}
    subject    = data.get("subject", "General Knowledge")
    topic      = data.get("topic", "")
    num_q      = min(int(data.get("num_questions", 5)), 10)
    difficulty = data.get("difficulty", "Medium")

    questions = ai_service.generate_quiz(
        subject=subject, topic=topic,
        class_level=user.class_level or "primary",
        sub_class=user.sub_class or "",
        num_q=num_q, difficulty=difficulty,
        language=user.language_name or "English",
    )
    if questions is None:
        return jsonify({"error": "AI quiz generation failed — check API keys"}), 500

    db = get_db_direct()
    try:
        # Ensure invite code uniqueness
        for _ in range(10):
            code = _gen_invite_code()
            if not db.query(Battle).filter_by(invite_code=code).first():
                break

        battle = Battle(
            invite_code=code,
            subject=subject,
            class_level=user.class_level,
            difficulty=difficulty,
            questions_json=json.dumps(questions),
            player1_id=user.id,
            player1_name=user.name,
            status="waiting",
        )
        db.add(battle)
        db.commit()
        db.refresh(battle)

        return jsonify({
            "battle_id": battle.id,
            "invite_code": code,
            "subject": subject,
            "num_questions": len(questions),
            "status": battle.status,
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/battle/join", methods=["POST"])
@require_learner
def battle_join(user):
    """
    Body: { invite_code: str }
    """
    data = request.get_json(force=True) or {}
    code = (data.get("invite_code") or "").strip().upper()
    if not code:
        return jsonify({"error": "invite_code is required"}), 400

    db = get_db_direct()
    try:
        battle = db.query(Battle).filter_by(invite_code=code).first()
        if not battle:
            return jsonify({"error": "Invalid invite code"}), 404
        if battle.status != "waiting":
            return jsonify({"error": "This battle is no longer accepting players", "status": battle.status}), 409
        if battle.player1_id == user.id:
            return jsonify({"error": "You can't join your own battle"}), 400

        battle.player2_id = user.id
        battle.player2_name = user.name
        battle.status = "active"
        battle.started_at = datetime.utcnow()
        db.commit()

        return jsonify({
            "battle_id": battle.id,
            "status": battle.status,
            "subject": battle.subject,
            "questions": json.loads(battle.questions_json or "[]"),
            "opponent_name": battle.player1_name,
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/battle/<int:battle_id>/state", methods=["GET"])
@require_learner
def battle_state(user, battle_id):
    """
    Poll endpoint — call every ~2s while in a battle.
    Returns both players' progress, scores, and overall status.
    """
    db = get_db_direct()
    try:
        battle = db.query(Battle).filter_by(id=battle_id).first()
        if not battle:
            return jsonify({"error": "Battle not found"}), 404
        if user.id not in (battle.player1_id, battle.player2_id):
            return jsonify({"error": "You are not a player in this battle"}), 403

        questions = json.loads(battle.questions_json or "[]")
        total_q = len(questions)

        p1_progress = _player_progress(db, battle_id, battle.player1_id, total_q)
        p2_progress = _player_progress(db, battle_id, battle.player2_id, total_q) if battle.player2_id else None

        # Check for completion + determine winner (only once, when both done)
        if (battle.status == "active" and p2_progress
                and p1_progress["answered"] == total_q
                and p2_progress["answered"] == total_q):
            _finalize_battle(db, battle, p1_progress, p2_progress)

        return jsonify({
            "battle_id": battle.id,
            "status": battle.status,
            "subject": battle.subject,
            "total_questions": total_q,
            "questions": questions if battle.status != "waiting" else [],
            "player1": {"id": battle.player1_id, "name": battle.player1_name, **p1_progress},
            "player2": (
                {"id": battle.player2_id, "name": battle.player2_name, **p2_progress}
                if battle.player2_id else None
            ),
            "winner_id": battle.winner_id,
            "your_id": user.id,
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/battle/<int:battle_id>/answer", methods=["POST"])
@require_learner
def battle_answer(user, battle_id):
    """
    Body: { question_index: int, chosen_index: int }
    """
    data = request.get_json(force=True) or {}
    q_index = data.get("question_index")
    chosen  = data.get("chosen_index")

    if q_index is None or chosen is None:
        return jsonify({"error": "question_index and chosen_index are required"}), 400

    db = get_db_direct()
    try:
        battle = db.query(Battle).filter_by(id=battle_id).first()
        if not battle:
            return jsonify({"error": "Battle not found"}), 404
        if user.id not in (battle.player1_id, battle.player2_id):
            return jsonify({"error": "You are not a player in this battle"}), 403
        if battle.status != "active":
            return jsonify({"error": f"Battle is not active (status={battle.status})"}), 409

        # Idempotency: don't allow re-answering the same question
        existing = db.query(BattleAnswer).filter_by(
            battle_id=battle_id, player_id=user.id, question_index=q_index
        ).first()
        if existing:
            return jsonify({"error": "Already answered this question",
                             "is_correct": existing.is_correct}), 409

        questions = json.loads(battle.questions_json or "[]")
        if q_index < 0 or q_index >= len(questions):
            return jsonify({"error": "question_index out of range"}), 400

        is_correct = (chosen == questions[q_index].get("correct"))

        ans = BattleAnswer(
            battle_id=battle_id, player_id=user.id,
            question_index=q_index, chosen_index=chosen,
            is_correct=is_correct,
        )
        db.add(ans)
        db.commit()

        return jsonify({"is_correct": is_correct, "correct_index": questions[q_index].get("correct")})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/battle/my-active", methods=["GET"])
@require_learner
def battle_my_active(user):
    """Returns the learner's most recent waiting/active battle, if any."""
    db = get_db_direct()
    try:
        battle = (
            db.query(Battle)
            .filter(
                ((Battle.player1_id == user.id) | (Battle.player2_id == user.id)),
                Battle.status.in_(["waiting", "active"])
            )
            .order_by(Battle.created_at.desc())
            .first()
        )
        if not battle:
            return jsonify({"active": False})
        return jsonify({
            "active": True,
            "battle_id": battle.id,
            "invite_code": battle.invite_code,
            "status": battle.status,
            "subject": battle.subject,
        })
    finally:
        db.close()


def _player_progress(db, battle_id: int, player_id: int, total_q: int) -> dict:
    answers = db.query(BattleAnswer).filter_by(
        battle_id=battle_id, player_id=player_id
    ).all()
    correct = sum(1 for a in answers if a.is_correct)
    last_answer_at = max((a.answered_at for a in answers), default=None)
    return {
        "answered": len(answers),
        "correct": correct,
        "score_pct": round((correct / total_q) * 100, 1) if total_q else 0,
        "last_answer_at": last_answer_at.isoformat() if last_answer_at else None,
    }


def _finalize_battle(db, battle: Battle, p1: dict, p2: dict):
    """
    Determines winner and marks battle finished. Awards leaderboard entries
    and points to both players (winner gets a bonus).
    Called once both players have answered all questions.
    """
    if p1["score_pct"] > p2["score_pct"]:
        winner_id = battle.player1_id
    elif p2["score_pct"] > p1["score_pct"]:
        winner_id = battle.player2_id
    else:
        # Tie on score → faster finisher wins; if both null, it's a draw
        t1, t2 = p1["last_answer_at"], p2["last_answer_at"]
        if t1 and t2:
            winner_id = battle.player1_id if t1 < t2 else battle.player2_id
        else:
            winner_id = None  # draw

    battle.winner_id = winner_id
    battle.status = "finished"
    battle.finished_at = datetime.utcnow()

    # Award points: 5 per correct answer + 25 bonus for winning
    for pid, prog in ((battle.player1_id, p1), (battle.player2_id, p2)):
        u = db.query(models.User).filter_by(id=pid).first()
        if not u:
            continue
        bonus = 25 if pid == winner_id else 0
        u.points = (u.points or 0) + prog["correct"] * 5 + bonus

        # Leaderboard entry
        lb = models.LeaderboardEntry(
            learner_name=u.name,
            learner_id=u.id,
            score_pct=prog["score_pct"],
            score_raw=f"{prog['correct']}/{len(json.loads(battle.questions_json or '[]'))}",
            subject=f"Battle: {battle.subject}",
            class_level=u.class_level,
            entry_type="quiz",
        )
        db.add(lb)

    db.commit()


# ════════════════════════════════════════════════════════════════════════════
# ── TEXTBOOK PHOTO SCANNER ────────────────────────────────────────────────
# POST /eduai/phase2/textbook/scan         → upload photo, get text+explanation+flashcards
# POST /eduai/phase2/textbook/quiz         → generate quiz from a previous scan
# GET  /eduai/phase2/textbook/history      → recent scans
# ════════════════════════════════════════════════════════════════════════════

# Reasonable cap to avoid huge base64 payloads / API costs (~8MB raw image)
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


@blueprint.route("/textbook/scan", methods=["POST"])
@require_learner
def textbook_scan(user):
    """
    Multipart form-data:
      file: image file (jpg/png/webp/gif)
      subject: optional str
    """
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file is required (multipart/form-data)"}), 400

    filename = f.filename or "photo.jpg"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        return jsonify({"error": "Unsupported image type — use jpg, png, webp, or gif"}), 400

    raw = f.read()
    if len(raw) > _MAX_IMAGE_BYTES:
        return jsonify({"error": "Image too large — max 8MB"}), 400
    if not raw:
        return jsonify({"error": "Empty file"}), 400

    image_b64 = base64.b64encode(raw).decode("utf-8")
    subject = request.form.get("subject", "")

    result = ai_service.scan_textbook_page(
        image_b64=image_b64,
        image_ext=ext,
        class_level=user.class_level or "primary",
        sub_class=user.sub_class or "",
        language=user.language_name or "English",
    )
    if result is None:
        return jsonify({
            "error": "Textbook scanning requires vision AI (ANTHROPIC_API_KEY). "
                     "Check your API key configuration."
        }), 503

    db = get_db_direct()
    try:
        scan = TextbookScan(
            learner_id=user.id,
            subject=subject,
            extracted_text=result.get("extracted_text", ""),
            explanation=result.get("explanation", ""),
            flashcards_json=json.dumps(result.get("flashcards", [])),
        )
        db.add(scan)

        # Award points for using the feature (encourages daily use)
        u = db.query(models.User).filter_by(id=user.id).first()
        if u:
            u.points = (u.points or 0) + 5

        db.commit()
        db.refresh(scan)

        return jsonify({
            "scan_id": scan.id,
            "extracted_text": result.get("extracted_text", ""),
            "explanation": result.get("explanation", ""),
            "flashcards": result.get("flashcards", []),
        })
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/textbook/quiz", methods=["POST"])
@require_learner
def textbook_quiz(user):
    """
    Body: { scan_id: int, num_questions?: int (default 5, max 15) }
    Generates a quiz from a previous scan's extracted text.
    """
    data = request.get_json(force=True) or {}
    scan_id = data.get("scan_id")
    num_q = min(int(data.get("num_questions", 5)), 15)

    if not scan_id:
        return jsonify({"error": "scan_id is required"}), 400

    db = get_db_direct()
    try:
        scan = db.query(TextbookScan).filter_by(id=scan_id, learner_id=user.id).first()
        if not scan:
            return jsonify({"error": "Scan not found"}), 404
        if not scan.extracted_text or scan.extracted_text.strip().lower() == "unreadable":
            return jsonify({"error": "This scan has no usable text to generate a quiz from"}), 400

        questions = ai_service.generate_quiz_from_text(
            source_text=scan.extracted_text,
            class_level=user.class_level or "primary",
            sub_class=user.sub_class or "",
            language=user.language_name or "English",
            num_q=num_q,
        )
        if questions is None:
            return jsonify({"error": "AI quiz generation failed — check API keys"}), 500

        scan.quiz_json = json.dumps(questions)
        db.commit()

        return jsonify({"questions": questions, "num_generated": len(questions)})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@blueprint.route("/textbook/history", methods=["GET"])
@require_learner
def textbook_history(user):
    db = get_db_direct()
    try:
        scans = (
            db.query(TextbookScan)
            .filter_by(learner_id=user.id)
            .order_by(TextbookScan.created_at.desc())
            .limit(20)
            .all()
        )
        return jsonify({"scans": [
            {
                "id": s.id,
                "subject": s.subject,
                "extracted_text_preview": (s.extracted_text or "")[:120],
                "has_flashcards": bool(s.flashcards_json and s.flashcards_json != "[]"),
                "has_quiz": bool(s.quiz_json),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in scans
        ]})
    finally:
        db.close()


@blueprint.route("/textbook/scan/<int:scan_id>", methods=["GET"])
@require_learner
def textbook_scan_detail(user, scan_id):
    db = get_db_direct()
    try:
        scan = db.query(TextbookScan).filter_by(id=scan_id, learner_id=user.id).first()
        if not scan:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "id": scan.id,
            "subject": scan.subject,
            "extracted_text": scan.extracted_text,
            "explanation": scan.explanation,
            "flashcards": json.loads(scan.flashcards_json or "[]"),
            "quiz": json.loads(scan.quiz_json) if scan.quiz_json else None,
            "created_at": scan.created_at.isoformat() if scan.created_at else None,
        })
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# REGISTRATION
# ════════════════════════════════════════════════════════════════════════════

def register_phase2_routes(app):
    """
    Call this inside api.py, alongside the other EduAI registrations:

        try:
            from eduai.app.routes.phase2_routes import register_phase2_routes
            register_phase2_routes(app)
            print("[API] ✅ Phase2 routes registered at /eduai/phase2")
        except Exception as e:
            print(f"[API] Phase2 routes skipped: {e}")
    """
    app.register_blueprint(blueprint)
    print("[EDUAI-PHASE2] ✅ Battle Mode / Textbook Scanner registered at /eduai/phase2")