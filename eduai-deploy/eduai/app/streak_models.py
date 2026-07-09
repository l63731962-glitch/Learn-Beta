"""
eduai/app/streak_models.py
Place this file at:  eduai/app/streak_models.py
(Same folder as models.py, database.py, community_models.py)

New tables for Phase 1 — Daily Challenges, Streaks, Study Timer, Parent Dashboard.
None of these touch existing tables. database.py needs ONE import line added
(see step 2 of the integration guide).
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
from eduai.app.database import Base


class DailyChallenge(Base):
    """One generated challenge per user per day. created fresh by a cron-like
    endpoint call (or lazily on first dashboard hit of the day)."""
    __tablename__ = 'eduai_daily_challenges'

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    challenge_date= Column(String(10), nullable=False, index=True)  # 'YYYY-MM-DD' (UTC)
    subject       = Column(String(100), nullable=True)
    topic         = Column(String(300), nullable=True)
    questions_json= Column(Text,        nullable=False)  # same shape as quiz questions
    is_completed  = Column(Boolean,     default=False)
    score_pct     = Column(Float,       nullable=True)
    completed_at  = Column(DateTime,    nullable=True)
    created_at    = Column(DateTime,    default=datetime.utcnow)


class StreakLog(Base):
    """One row per day a user completes ANY qualifying activity (challenge,
    quiz, study session). Used to compute the streak and award badges."""
    __tablename__ = 'eduai_streak_log'

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    activity_date = Column(String(10), nullable=False, index=True)  # 'YYYY-MM-DD' UTC
    source      = Column(String(30), nullable=True)  # 'daily_challenge' | 'quiz' | 'study_timer'
    created_at  = Column(DateTime, default=datetime.utcnow)


class StudySession(Base):
    """Pomodoro-style study timer sessions. Coins awarded on completion."""
    __tablename__ = 'eduai_study_sessions'

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    subject      = Column(String(100), nullable=True)
    duration_min = Column(Integer, default=25)
    coins_earned = Column(Integer, default=0)
    completed    = Column(Boolean, default=False)
    started_at   = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class ParentLink(Base):
    """Links a parent account (a User with role='parent' OR a lightweight
    parent record) to a learner. We reuse the existing User table — parents
    register normally with role='parent' (added to UserRole enum), then
    link to a child via the child's referral/link code.

    link_code is generated for the LEARNER; the parent enters it once."""
    __tablename__ = 'eduai_parent_links'

    id          = Column(Integer, primary_key=True, index=True)
    parent_id   = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    learner_id  = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class LearnerLinkCode(Base):
    """Each learner gets a persistent link code that a parent enters to
    connect. One row per learner, generated on first request."""
    __tablename__ = 'eduai_learner_link_codes'

    id          = Column(Integer, primary_key=True, index=True)
    learner_id  = Column(Integer, ForeignKey('eduai_users.id'), unique=True, nullable=False, index=True)
    link_code   = Column(String(20), unique=True, nullable=False, index=True)
    created_at  = Column(DateTime, default=datetime.utcnow)