"""
eduai/app/community_models.py
Place this file at:  eduai/app/community_models.py
(Same folder as models.py and database.py)
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
from eduai.app.database import Base


class CommunityMessage(Base):
    __tablename__ = 'community_messages'
    id             = Column(Integer, primary_key=True, index=True)
    org_id         = Column(Integer, ForeignKey('eduai_organizations.id'), nullable=True, index=True)
    group_type     = Column(String(20),  nullable=False)
    sender_id      = Column(Integer,     nullable=False)
    sender_name    = Column(String(120), nullable=False)
    sender_school  = Column(String(200), nullable=True)
    message_text   = Column(Text,        nullable=False)
    message_type   = Column(String(20),  default='text')
    attachment_url = Column(String(500), nullable=True)
    reactions_json = Column(Text,        default='{}')
    reply_to_id    = Column(Integer, ForeignKey('community_messages.id'), nullable=True)
    is_deleted     = Column(Boolean,     default=False)
    timestamp      = Column(DateTime,    default=datetime.utcnow)


class CommunityResource(Base):
    __tablename__ = 'community_resources'
    id              = Column(Integer,     primary_key=True, index=True)
    org_id          = Column(Integer, ForeignKey('eduai_organizations.id'), nullable=True, index=True)
    group_type      = Column(String(20),  nullable=False)
    shared_by_id    = Column(Integer,     nullable=False)
    shared_by_name  = Column(String(120), nullable=False)
    resource_type   = Column(String(30),  nullable=True)
    title           = Column(String(300), nullable=False)
    subject         = Column(String(100), nullable=True)
    class_level     = Column(String(50),  nullable=True)
    content_text    = Column(Text,        nullable=True)
    file_url        = Column(String(500), nullable=True)
    likes_count     = Column(Integer,     default=0)
    downloads_count = Column(Integer,     default=0)
    target_group    = Column(String(20),  default='both')
    timestamp       = Column(DateTime,    default=datetime.utcnow)


class CommunityAnnouncement(Base):
    __tablename__ = 'community_announcements'
    id           = Column(Integer,     primary_key=True, index=True)
    org_id       = Column(Integer, ForeignKey('eduai_organizations.id'), nullable=True, index=True)
    author_id    = Column(Integer,     nullable=False)
    author_name  = Column(String(120), nullable=False)
    title        = Column(String(300), nullable=False)
    body         = Column(Text,        nullable=False)
    priority     = Column(String(20),  default='normal')
    target_group = Column(String(20),  default='both')
    expires_at   = Column(DateTime,    nullable=True)
    is_active    = Column(Boolean,     default=True)
    created_at   = Column(DateTime,    default=datetime.utcnow)


class GroupTestAssignment(Base):
    __tablename__ = 'group_test_assignments'
    id                 = Column(Integer,     primary_key=True, index=True)
    org_id             = Column(Integer, ForeignKey('eduai_organizations.id'), nullable=True, index=True)
    title              = Column(String(300), nullable=False)
    subject            = Column(String(100), nullable=True)
    questions_json     = Column(Text,        nullable=False)
    assigned_by_id     = Column(Integer,     nullable=False)
    assigned_by_name   = Column(String(120), nullable=False)
    target_class_level = Column(String(50),  nullable=True)
    deadline           = Column(DateTime,    nullable=True)
    time_limit_mins    = Column(Integer,     default=60)
    is_active          = Column(Boolean,     default=True)
    created_at         = Column(DateTime,    default=datetime.utcnow)


class GroupTestSubmission(Base):
    __tablename__ = 'group_test_submissions'
    id            = Column(Integer,     primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey('group_test_assignments.id'), nullable=True)
    learner_id    = Column(Integer,     nullable=False)
    learner_name  = Column(String(120), nullable=False)
    answers_json  = Column(Text,        nullable=True)
    score_pct     = Column(Float,       nullable=True)
    submitted_at  = Column(DateTime,    default=datetime.utcnow)
    feedback_text = Column(Text,        nullable=True)


class HomeworkQuestion(Base):
    __tablename__ = 'homework_questions'
    id            = Column(Integer,     primary_key=True, index=True)
    org_id        = Column(Integer, ForeignKey('eduai_organizations.id'), nullable=True, index=True)
    asker_id      = Column(Integer,     nullable=False)
    asker_name    = Column(String(120), nullable=False)
    subject       = Column(String(100), nullable=True)
    class_level   = Column(String(50),  nullable=True)
    question_text = Column(Text,        nullable=False)
    is_resolved   = Column(Boolean,     default=False)
    created_at    = Column(DateTime,    default=datetime.utcnow)


class HomeworkAnswer(Base):
    __tablename__ = 'homework_answers'
    id            = Column(Integer,     primary_key=True, index=True)
    question_id   = Column(Integer, ForeignKey('homework_questions.id'), nullable=True)
    answerer_id   = Column(Integer,     nullable=False)
    answerer_name = Column(String(120), nullable=False)
    answer_text   = Column(Text,        nullable=False)
    is_official   = Column(Boolean,     default=False)
    upvotes       = Column(Integer,     default=0)
    created_at    = Column(DateTime,    default=datetime.utcnow)


class CommunityNotification(Base):
    __tablename__ = 'community_notifications'
    id          = Column(Integer,     primary_key=True, index=True)
    user_id     = Column(Integer,     nullable=False, index=True)
    notif_type  = Column(String(50),  nullable=True)
    title       = Column(String(200), nullable=True)
    body        = Column(Text,        nullable=True)
    is_read     = Column(Boolean,     default=False)
    created_at  = Column(DateTime,    default=datetime.utcnow)