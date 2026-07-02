"""
eduai/app/routes/community_routes.py
Registered onto OMEGA Flask app in interfaces/api.py via:
    from eduai.app.routes.community_routes import register_community_routes
    register_community_routes(app)
URL prefix: /community  →  routes at /community/*
"""
import json
from datetime import datetime
from functools import wraps
from flask import Blueprint, request, jsonify
from eduai.app.database import get_db_direct
from eduai.app import models
from eduai.app.community_models import (
    CommunityMessage, CommunityResource, CommunityAnnouncement,
    GroupTestAssignment, GroupTestSubmission,
    HomeworkQuestion, HomeworkAnswer, CommunityNotification,
)
from eduai.app.service import auth_service

community_bp = Blueprint('community', __name__, url_prefix='/community')


# ── Auth helpers (same pattern as eduai_routes.py) ────────────────────────────

def _get_token():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return (request.json or {}).get('token', '') if request.is_json else ''


def _current_user(db=None):
    close_db = db is None
    if close_db:
        db = get_db_direct()
    try:
        token = _get_token()
        if not token:
            return None, (jsonify({'error': 'Unauthorized'}), 401)
        td = auth_service.verify_token(token)
        if not td:
            return None, (jsonify({'error': 'Token invalid or expired'}), 401)
        user = db.query(models.User).filter_by(id=td['user_id']).first()
        if not user:
            return None, (jsonify({'error': 'User not found'}), 404)
        return user, None
    finally:
        if close_db:
            db.close()


def _is_teacher(user):
    role = user.role
    if hasattr(role, 'value'):
        role = role.value
    return str(role).lower() == 'teacher'


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, err = _current_user()
        if err:
            return err
        return f(user, *args, **kwargs)
    return wrapper


def require_active_subscription(f):
    """Stack directly under @require_auth. Blocks access once the 7-day
    trial has ended and no active PayPal subscription exists."""
    @wraps(f)
    def wrapper(user, *args, **kwargs):
        db = get_db_direct()
        try:
            fresh_user = db.query(models.User).filter_by(id=user.id).first()
            if not fresh_user:
                return jsonify({'error': 'User not found'}), 404
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


def _org_filter(query, model, user):
    """
    Scope a query to the user's org.
    - org users  (org_id set)  → filter to their org only
    - personal users (org_id None) → filter to rows where org_id IS NULL
      so personal accounts form their own isolated pool.
    """
    if user.org_id:
        return query.filter(model.org_id == user.org_id)
    else:
        return query.filter(model.org_id == None)  # noqa: E711


# ── GROUP CHAT ────────────────────────────────────────────────────────────────

@community_bp.route('/messages/<group_type>', methods=['GET'])
@require_auth
@require_active_subscription
def get_messages(user, group_type):
    after_id = request.args.get('after', 0, type=int)
    limit    = min(request.args.get('limit', 50, type=int), 100)
    db = get_db_direct()
    try:
        q = db.query(CommunityMessage).filter(
            CommunityMessage.group_type == group_type,
            CommunityMessage.is_deleted == False
        )
        q = _org_filter(q, CommunityMessage, user)
        if after_id:
            q = q.filter(CommunityMessage.id > after_id)
        msgs = q.order_by(CommunityMessage.timestamp.asc()).limit(limit).all()
        return jsonify({'messages': [{
            'id': m.id, 'sender_id': m.sender_id, 'sender_name': m.sender_name,
            'sender_school': m.sender_school or '', 'message_text': m.message_text,
            'message_type': m.message_type, 'attachment_url': m.attachment_url,
            'reactions': json.loads(m.reactions_json or '{}'),
            'reply_to_id': m.reply_to_id,
            'timestamp': m.timestamp.isoformat(),
        } for m in msgs]})
    finally:
        db.close()


@community_bp.route('/message/send', methods=['POST'])
@require_auth
@require_active_subscription
def send_message(user):
    data = request.get_json(force=True) or {}
    text = (data.get('message_text') or '').strip()
    if not text:
        return jsonify({'error': 'Message cannot be empty'}), 400
    db = get_db_direct()
    try:
        msg = CommunityMessage(
            org_id        = user.org_id,
            group_type    = data.get('group_type', 'teachers'),
            sender_id     = user.id,
            sender_name   = user.name,
            sender_school = getattr(user, 'school', '') or '',
            message_text  = text,
            message_type  = data.get('message_type', 'text'),
            attachment_url= data.get('attachment_url'),
            reply_to_id   = data.get('reply_to_id'),
        )
        db.add(msg); db.commit(); db.refresh(msg)
        return jsonify({'success': True, 'id': msg.id, 'timestamp': msg.timestamp.isoformat()})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@community_bp.route('/message/react', methods=['POST'])
@require_auth
@require_active_subscription
def react_message(user):
    data  = request.get_json(force=True) or {}
    db = get_db_direct()
    try:
        msg = db.query(CommunityMessage).get(data.get('message_id'))
        if not msg:
            return jsonify({'error': 'Message not found'}), 404
        reactions = json.loads(msg.reactions_json or '{}')
        emoji = data.get('emoji', '👍')
        reactions[emoji] = reactions.get(emoji, 0) + 1
        msg.reactions_json = json.dumps(reactions)
        db.commit()
        return jsonify({'success': True, 'reactions': reactions})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@community_bp.route('/message/delete/<int:msg_id>', methods=['DELETE'])
@require_auth
@require_active_subscription
def delete_message(user, msg_id):
    db = get_db_direct()
    try:
        msg = db.query(CommunityMessage).get(msg_id)
        if not msg:
            return jsonify({'error': 'Not found'}), 404
        if msg.sender_id != user.id and not _is_teacher(user):
            return jsonify({'error': 'Forbidden'}), 403
        msg.is_deleted = True; db.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── RESOURCES ─────────────────────────────────────────────────────────────────

@community_bp.route('/resources/<group_type>', methods=['GET'])
@require_auth
@require_active_subscription
def get_resources(user, group_type):
    db = get_db_direct()
    try:
        q = db.query(CommunityResource).filter(
            (CommunityResource.group_type == group_type) |
            (CommunityResource.target_group == 'both')
        )
        q = _org_filter(q, CommunityResource, user)
        if request.args.get('subject'):
            q = q.filter(CommunityResource.subject == request.args['subject'])
        if request.args.get('type'):
            q = q.filter(CommunityResource.resource_type == request.args['type'])
        resources = q.order_by(CommunityResource.timestamp.desc()).limit(50).all()
        return jsonify({'resources': [{
            'id': r.id, 'shared_by_name': r.shared_by_name,
            'resource_type': r.resource_type, 'title': r.title,
            'subject': r.subject or '', 'class_level': r.class_level or '',
            'content_text': r.content_text or '', 'file_url': r.file_url,
            'likes_count': r.likes_count, 'downloads_count': r.downloads_count,
            'timestamp': r.timestamp.isoformat(),
        } for r in resources]})
    finally:
        db.close()


@community_bp.route('/resource/share', methods=['POST'])
@require_auth
@require_active_subscription
def share_resource(user):
    data = request.get_json(force=True) or {}
    db = get_db_direct()
    try:
        res = CommunityResource(
            org_id        = user.org_id,
            group_type    = data.get('group_type', 'teachers'),
            shared_by_id  = user.id, shared_by_name = user.name,
            resource_type = data.get('resource_type', 'lesson_note'),
            title         = data.get('title', 'Untitled'),
            subject       = data.get('subject', ''),
            class_level   = data.get('class_level', ''),
            content_text  = data.get('content_text', ''),
            file_url      = data.get('file_url'),
            target_group  = data.get('target_group', 'both'),
        )
        db.add(res); db.commit(); db.refresh(res)
        return jsonify({'success': True, 'id': res.id})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@community_bp.route('/resource/like/<int:res_id>', methods=['POST'])
def like_resource(res_id):
    db = get_db_direct()
    try:
        r = db.query(CommunityResource).get(res_id)
        if not r: return jsonify({'error': 'Not found'}), 404
        r.likes_count = (r.likes_count or 0) + 1; db.commit()
        return jsonify({'likes_count': r.likes_count})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── ANNOUNCEMENTS ─────────────────────────────────────────────────────────────

@community_bp.route('/announcements', methods=['GET'])
@require_auth
@require_active_subscription
def get_announcements(user):
    now = datetime.utcnow()
    db = get_db_direct()
    try:
        q = db.query(CommunityAnnouncement).filter(
            CommunityAnnouncement.is_active == True
        )
        q = _org_filter(q, CommunityAnnouncement, user)
        all_ann = q.order_by(CommunityAnnouncement.created_at.desc()).limit(20).all()
        active = [a for a in all_ann if a.expires_at is None or a.expires_at > now]
        return jsonify({'announcements': [{
            'id': a.id, 'author_name': a.author_name, 'title': a.title,
            'body': a.body, 'priority': a.priority, 'target_group': a.target_group,
            'created_at': a.created_at.isoformat(),
        } for a in active]})
    finally:
        db.close()


@community_bp.route('/announcement/create', methods=['POST'])
@require_auth
@require_active_subscription
def create_announcement(user):
    if not _is_teacher(user):
        return jsonify({'error': 'Teachers only'}), 403
    data = request.get_json(force=True) or {}
    if not data.get('title') or not data.get('body'):
        return jsonify({'error': 'title and body are required'}), 400
    db = get_db_direct()
    try:
        ann = CommunityAnnouncement(
            org_id      = user.org_id,
            author_id   = user.id, author_name = user.name,
            title       = data['title'], body = data['body'],
            priority    = data.get('priority', 'normal'),
            target_group= data.get('target_group', 'both'),
        )
        if data.get('expires_at'):
            try: ann.expires_at = datetime.fromisoformat(data['expires_at'])
            except Exception: pass
        db.add(ann); db.commit(); db.refresh(ann)
        return jsonify({'success': True, 'id': ann.id})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── TEST DISTRIBUTION ─────────────────────────────────────────────────────────

@community_bp.route('/test/publish', methods=['POST'])
@require_auth
@require_active_subscription
def publish_test(user):
    if not _is_teacher(user):
        return jsonify({'error': 'Teachers only'}), 403
    data = request.get_json(force=True) or {}
    questions = data.get('questions', [])
    if not questions:
        return jsonify({'error': 'questions array is required'}), 400
    db = get_db_direct()
    try:
        a = GroupTestAssignment(
            org_id             = user.org_id,
            title              = data.get('title', 'Untitled Test'),
            subject            = data.get('subject', ''),
            questions_json     = json.dumps(questions),
            assigned_by_id    = user.id, assigned_by_name = user.name,
            target_class_level = data.get('target_class_level', ''),
            time_limit_mins    = data.get('time_limit_mins', 60),
        )
        if data.get('deadline'):
            try: a.deadline = datetime.fromisoformat(data['deadline'])
            except Exception: pass
        db.add(a); db.commit(); db.refresh(a)
        return jsonify({'success': True, 'id': a.id})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@community_bp.route('/test/assignments', methods=['GET'])
@require_auth
@require_active_subscription
def get_assignments(user):
    db = get_db_direct()
    try:
        q = db.query(GroupTestAssignment).filter_by(is_active=True)
        q = _org_filter(q, GroupTestAssignment, user)
        assignments = q.order_by(GroupTestAssignment.created_at.desc()).limit(20).all()
        return jsonify({'assignments': [{
            'id': a.id, 'title': a.title, 'subject': a.subject or '',
            'assigned_by_name': a.assigned_by_name,
            'target_class_level': a.target_class_level or '',
            'time_limit_mins': a.time_limit_mins,
            'deadline': a.deadline.isoformat() if a.deadline else None,
            'created_at': a.created_at.isoformat(),
        } for a in assignments]})
    finally:
        db.close()


@community_bp.route('/test/assignment/<int:assignment_id>', methods=['GET'])
@require_auth
@require_active_subscription
def get_assignment_questions(user, assignment_id):
    db = get_db_direct()
    try:
        a = db.query(GroupTestAssignment).get(assignment_id)
        if not a: return jsonify({'error': 'Not found'}), 404
        return jsonify({'assignment': {
            'id': a.id, 'title': a.title, 'subject': a.subject or '',
            'questions': json.loads(a.questions_json or '[]'),
            'time_limit_mins': a.time_limit_mins,
            'deadline': a.deadline.isoformat() if a.deadline else None,
        }})
    finally:
        db.close()


@community_bp.route('/test/submit', methods=['POST'])
@require_auth
@require_active_subscription
def submit_test(user):
    data = request.get_json(force=True) or {}
    assignment_id = data.get('assignment_id')
    if not assignment_id:
        return jsonify({'error': 'assignment_id is required'}), 400
    db = get_db_direct()
    try:
        assignment = db.query(GroupTestAssignment).get(assignment_id)
        if not assignment: return jsonify({'error': 'Assignment not found'}), 404
        questions = json.loads(assignment.questions_json or '[]')
        answers   = data.get('answers', {})
        correct = 0
        for i, q in enumerate(questions):
            correct_letter = next(
                (o.get('letter') for o in q.get('opts', []) if o.get('correct')), None
            ) or q.get('ans')
            if correct_letter and answers.get(str(i)) == correct_letter:
                correct += 1
        total     = len(questions)
        score_pct = round((correct / total) * 100) if total else 0
        sub = GroupTestSubmission(
            assignment_id = assignment.id, learner_id = user.id,
            learner_name  = user.name, answers_json = json.dumps(answers),
            score_pct     = score_pct,
        )
        db.add(sub); db.commit()
        return jsonify({'success': True, 'score_pct': score_pct, 'correct': correct, 'total': total})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@community_bp.route('/test/submissions/<int:assignment_id>', methods=['GET'])
@require_auth
@require_active_subscription
def get_submissions(user, assignment_id):
    if not _is_teacher(user):
        return jsonify({'error': 'Teachers only'}), 403
    db = get_db_direct()
    try:
        subs = db.query(GroupTestSubmission).filter_by(
            assignment_id=assignment_id
        ).order_by(GroupTestSubmission.submitted_at.desc()).all()
        return jsonify({'submissions': [{
            'id': s.id, 'learner_name': s.learner_name,
            'score_pct': s.score_pct,
            'submitted_at': s.submitted_at.isoformat(),
        } for s in subs]})
    finally:
        db.close()


# ── HOMEWORK BOARD ────────────────────────────────────────────────────────────

@community_bp.route('/homework/questions', methods=['GET'])
@require_auth
@require_active_subscription
def get_homework(user):
    db = get_db_direct()
    try:
        q = db.query(HomeworkQuestion)
        q = _org_filter(q, HomeworkQuestion, user)
        if request.args.get('subject'):
            q = q.filter(HomeworkQuestion.subject == request.args['subject'])
        questions = q.order_by(HomeworkQuestion.created_at.desc()).limit(30).all()
        return jsonify({'questions': [{
            'id': q.id, 'asker_name': q.asker_name, 'subject': q.subject or '',
            'class_level': q.class_level or '', 'question_text': q.question_text,
            'is_resolved': q.is_resolved, 'created_at': q.created_at.isoformat(),
        } for q in questions]})
    finally:
        db.close()


@community_bp.route('/homework/ask', methods=['POST'])
@require_auth
@require_active_subscription
def ask_homework(user):
    data = request.get_json(force=True) or {}
    text = (data.get('question_text') or '').strip()
    if not text: return jsonify({'error': 'question_text is required'}), 400
    db = get_db_direct()
    try:
        q = HomeworkQuestion(
            org_id        = user.org_id,
            asker_id      = user.id, asker_name = user.name,
            subject       = data.get('subject', ''),
            class_level   = data.get('class_level', '') or getattr(user, 'class_level', ''),
            question_text = text,
        )
        db.add(q); db.commit(); db.refresh(q)
        return jsonify({'success': True, 'id': q.id})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@community_bp.route('/homework/answer', methods=['POST'])
@require_auth
@require_active_subscription
def answer_homework(user):
    data = request.get_json(force=True) or {}
    qid  = data.get('question_id')
    text = (data.get('answer_text') or '').strip()
    if not qid or not text:
        return jsonify({'error': 'question_id and answer_text required'}), 400
    db = get_db_direct()
    try:
        a = HomeworkAnswer(
            question_id   = qid, answerer_id = user.id,
            answerer_name = user.name, answer_text = text,
            is_official   = _is_teacher(user),
        )
        db.add(a)
        if a.is_official:
            q = db.query(HomeworkQuestion).get(qid)
            if q: q.is_resolved = True
        db.commit(); db.refresh(a)
        return jsonify({'success': True, 'id': a.id})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@community_bp.route('/homework/answers/<int:question_id>', methods=['GET'])
def get_answers(question_id):
    db = get_db_direct()
    try:
        answers = db.query(HomeworkAnswer).filter_by(
            question_id=question_id
        ).order_by(
            HomeworkAnswer.is_official.desc(), HomeworkAnswer.upvotes.desc()
        ).all()
        return jsonify({'answers': [{
            'id': a.id, 'answerer_name': a.answerer_name,
            'answer_text': a.answer_text, 'is_official': a.is_official,
            'upvotes': a.upvotes, 'created_at': a.created_at.isoformat(),
        } for a in answers]})
    finally:
        db.close()


@community_bp.route('/homework/upvote/<int:answer_id>', methods=['POST'])
def upvote_answer(answer_id):
    db = get_db_direct()
    try:
        a = db.query(HomeworkAnswer).get(answer_id)
        if not a: return jsonify({'error': 'Not found'}), 404
        a.upvotes = (a.upvotes or 0) + 1; db.commit()
        return jsonify({'upvotes': a.upvotes})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── NOTIFICATIONS ─────────────────────────────────────────────────────────────

@community_bp.route('/notifications', methods=['GET'])
@require_auth
@require_active_subscription
def get_notifications(user):
    db = get_db_direct()
    try:
        notifs = db.query(CommunityNotification).filter_by(
            user_id=user.id
        ).order_by(CommunityNotification.created_at.desc()).limit(30).all()
        unread = sum(1 for n in notifs if not n.is_read)
        return jsonify({
            'notifications': [{
                'id': n.id, 'type': n.notif_type, 'title': n.title,
                'body': n.body, 'is_read': n.is_read,
                'created_at': n.created_at.isoformat(),
            } for n in notifs],
            'unread': unread,
        })
    finally:
        db.close()


@community_bp.route('/notifications/read', methods=['POST'])
@require_auth
@require_active_subscription
def mark_notifications_read(user):
    db = get_db_direct()
    try:
        db.query(CommunityNotification).filter_by(
            user_id=user.id, is_read=False
        ).update({'is_read': True})
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.rollback(); return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── BLUEPRINT REGISTRATION ────────────────────────────────────────────────────

def register_community_routes(app):
    """
    Call this in interfaces/api.py right after register_eduai_routes(app):

        try:
            from eduai.app.routes.community_routes import register_community_routes
            register_community_routes(app)
            print("[API] ✅ Community routes registered at /community/*")
        except Exception as e:
            print(f"[API] Community routes skipped: {e}")
    """
    app.register_blueprint(community_bp)
    print('[COMMUNITY] ✅ Community routes registered at /community/*')
