"""
eduai/app/models.py
SQLAlchemy ORM models for AIR-EduAI platform.
All tables prefixed with 'eduai_' to avoid clashes with OMEGA tables.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean,
    DateTime, ForeignKey, JSON, Enum as SAEnum
)
from sqlalchemy.orm import relationship
from eduai.app.database import Base
import enum

class AccountType(str, enum.Enum):
    personal     = "personal"
    organization = "organization"


class Organization(Base):
    __tablename__ = "eduai_organizations"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(300), nullable=False)
    admin_code    = Column(String(20), unique=True, nullable=False, index=True)
    referral_code = Column(String(20), unique=True, nullable=False, index=True)
    created_by    = Column(Integer, ForeignKey("eduai_users.id"), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    members = relationship("User", back_populates="organization", foreign_keys="User.org_id")

# ── Enums ──────────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    teacher = "teacher"
    learner = "learner"
    parent  = "parent"

class ClassLevel(str, enum.Enum):
    kg         = "kg"
    primary    = "primary"
    jss        = "jss"
    sss        = "sss"
    university = "university"

class DifficultyLevel(str, enum.Enum):
    easy      = "Easy"
    medium    = "Medium"
    hard      = "Hard"
    mixed     = "Mixed"

class TestFormat(str, enum.Enum):
    cbt      = "cbt"
    download = "download"


# ── User ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "eduai_users"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(200), nullable=False)
    email         = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    role          = Column(SAEnum(UserRole), nullable=False)
    class_level   = Column(String(20), nullable=True)   # "primary", "jss", etc.
    sub_class     = Column(String(30), nullable=True)   # "primary-3", "jss-2", etc.
    school        = Column(String(300), nullable=True)
    language      = Column(String(10), default="en")
    language_name = Column(String(100), default="English")
    streak        = Column(Integer, default=0)
    points        = Column(Integer, default=0)
    quizzes_done  = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)
    last_login    = Column(DateTime, nullable=True)

    # Relationships
    lesson_notes  = relationship("LessonNote",  back_populates="teacher", cascade="all, delete-orphan")
    test_sessions = relationship("TestSession",  back_populates="teacher", cascade="all, delete-orphan")
    quiz_results  = relationship("QuizResult",   back_populates="learner", cascade="all, delete-orphan")
    game_sessions = relationship("GameSession",  back_populates="learner", cascade="all, delete-orphan")
    sow_uploads   = relationship("SchemeOfWork", back_populates="teacher", cascade="all, delete-orphan")
    organization  = relationship("Organization", back_populates="members", foreign_keys="User.org_id")
    account_type  = Column(SAEnum(AccountType), default=AccountType.personal, nullable=False)
    org_id        = Column(Integer, ForeignKey("eduai_organizations.id"), nullable=True)
    is_admin      = Column(Boolean, default=False, nullable=False)
    last_active   = Column(DateTime, nullable=True)

    # ── Phase 1 additions: streaks, coins, daily challenge tracking ────────
    coins              = Column(Integer, default=0)
    last_activity_date= Column(String(10), nullable=True)  # 'YYYY-MM-DD' UTC, for streak calc

# ── Subscription / Paywall ───────────────────────────────────────────
    subscription_status = Column(String(20), default="trial")  
    # "trial" | "active" | "past_due" | "cancelled" | "expired"
    trial_started_at    = Column(DateTime, default=datetime.utcnow)
    trial_ends_at        = Column(DateTime, nullable=True)
    paypal_subscription_id = Column(String(100), nullable=True, index=True)
    paypal_plan_id       = Column(String(100), nullable=True)
    payment_provider        = Column(String(20), nullable=True)  # 'paypal' | 'paystack'
    paystack_customer_code  = Column(String(100), nullable=True, index=True)
    paystack_subscription_code = Column(String(100), nullable=True)
    paystack_plan_code      = Column(String(100), nullable=True)
    paystack_email_token    = Column(String(200), nullable=True)  # required by Paystack to cancel
    subscription_updated_at = Column(DateTime, nullable=True)

# ── Lesson Note ─────────────────────────────────────────────────────────────

class LessonNote(Base):
    __tablename__ = "eduai_lesson_notes"

    id           = Column(Integer, primary_key=True, index=True)
    teacher_id   = Column(Integer, ForeignKey("eduai_users.id"), nullable=False)
    subject      = Column(String(200), nullable=False)
    class_level  = Column(String(20),  nullable=False)
    sub_class    = Column(String(30),  nullable=True)
    topic        = Column(String(500), nullable=False)
    duration     = Column(String(50),  default="45 minutes")
    curriculum   = Column(String(200), default="Nigerian (NERDC)")
    language     = Column(String(10),  default="en")
    language_name= Column(String(100), default="English")
    content      = Column(Text,        nullable=False)   # full markdown content
    created_at   = Column(DateTime,    default=datetime.utcnow)

    teacher = relationship("User", back_populates="lesson_notes")


# ── Scheme of Work ──────────────────────────────────────────────────────────

class SchemeOfWork(Base):
    __tablename__ = "eduai_schemes"

    id           = Column(Integer, primary_key=True, index=True)
    teacher_id   = Column(Integer, ForeignKey("eduai_users.id"), nullable=False)
    subject      = Column(String(200), nullable=False)
    class_level  = Column(String(20),  nullable=False)
    curriculum   = Column(String(200), default="Nigerian (NERDC)")
    filename     = Column(String(300), nullable=True)
    topics       = Column(JSON,        default=list)   # [{week,topic,objectives}]
    notes_count  = Column(Integer,     default=0)      # how many notes generated
    created_at   = Column(DateTime,    default=datetime.utcnow)

    teacher = relationship("User", back_populates="sow_uploads")


# ── Test Session ────────────────────────────────────────────────────────────

class TestSession(Base):
    __tablename__ = "eduai_test_sessions"

    id           = Column(Integer, primary_key=True, index=True)
    teacher_id   = Column(Integer, ForeignKey("eduai_users.id"), nullable=False)
    subject      = Column(String(200), nullable=False)
    class_level  = Column(String(20),  nullable=False)
    sub_class    = Column(String(30),  nullable=True)
    topic        = Column(String(500), nullable=True)
    num_questions= Column(Integer,     default=20)
    difficulty   = Column(String(20),  default="Medium")
    duration_min = Column(Integer,     default=60)
    format       = Column(String(20),  default="cbt")   # "cbt" | "download"
    language     = Column(String(10),  default="en")
    questions    = Column(JSON,        default=list)    # parsed question objects
    raw_content  = Column(Text,        nullable=True)   # raw AI output
    pin_code     = Column(String(10),  nullable=True)   # for CBT session link
    created_at   = Column(DateTime,    default=datetime.utcnow)

    teacher  = relationship("User",       back_populates="test_sessions")
    results  = relationship("TestResult", back_populates="session",  cascade="all, delete-orphan")


# ── Test Result ─────────────────────────────────────────────────────────────

class TestResult(Base):
    __tablename__ = "eduai_test_results"

    id            = Column(Integer, primary_key=True, index=True)
    session_id    = Column(Integer, ForeignKey("eduai_test_sessions.id"), nullable=False)
    student_name  = Column(String(200), nullable=False)
    student_class = Column(String(100), nullable=True)
    adm_number    = Column(String(100), nullable=True)
    gender        = Column(String(20),  nullable=True)
    score_pct     = Column(Float,       default=0.0)     # 0-100
    score_raw     = Column(String(30),  nullable=True)   # e.g. "15/20"
    answers       = Column(JSON,        default=dict)    # {q_index: letter_chosen}
    time_taken_s  = Column(Integer,     nullable=True)   # seconds
    submitted_at  = Column(DateTime,    default=datetime.utcnow)

    session = relationship("TestSession", back_populates="results")


# ── Quiz Result (Learner self-quiz) ─────────────────────────────────────────

class QuizResult(Base):
    __tablename__ = "eduai_quiz_results"

    id           = Column(Integer, primary_key=True, index=True)
    learner_id   = Column(Integer, ForeignKey("eduai_users.id"), nullable=False)
    subject      = Column(String(200), nullable=True)
    topic        = Column(String(500), nullable=True)
    class_level  = Column(String(20),  nullable=True)
    difficulty   = Column(String(20),  nullable=True)
    num_questions= Column(Integer,     default=10)
    score_pct    = Column(Float,       default=0.0)
    score_raw    = Column(String(30),  nullable=True)
    mode         = Column(String(20),  default="practice")  # "practice" | "test"
    created_at   = Column(DateTime,    default=datetime.utcnow)

    learner = relationship("User", back_populates="quiz_results")


# ── Leaderboard Entry ────────────────────────────────────────────────────────

class LeaderboardEntry(Base):
    __tablename__ = "eduai_leaderboard"

    id           = Column(Integer, primary_key=True, index=True)
    learner_name = Column(String(200), nullable=False)
    learner_id   = Column(Integer, nullable=True)   # optional — anonymous allowed
    org_id       = Column(Integer, ForeignKey("eduai_organizations.id"), nullable=True, index=True)  # ← school scope
    score_pct    = Column(Float,   default=0.0)
    score_raw    = Column(String(30), nullable=True)
    subject      = Column(String(200), nullable=True)
    class_level  = Column(String(20),  nullable=True)
    entry_type   = Column(String(20),  default="quiz")   # "quiz" | "cbt"
    created_at   = Column(DateTime,    default=datetime.utcnow)


# ── Game Session ─────────────────────────────────────────────────────────────

class GameSession(Base):
    __tablename__ = "eduai_game_sessions"

    id          = Column(Integer, primary_key=True, index=True)
    learner_id  = Column(Integer, ForeignKey("eduai_users.id"), nullable=False)
    game_id     = Column(String(50),  nullable=False)   # "alphabet", "mathrace", etc.
    game_title  = Column(String(200), nullable=True)
    score       = Column(Integer,     default=0)
    class_level = Column(String(20),  nullable=True)
    played_at   = Column(DateTime,    default=datetime.utcnow)

    learner = relationship("User", back_populates="game_sessions")


# ── Language Preference ───────────────────────────────────────────────────────

class LanguagePreference(Base):
    __tablename__ = "eduai_language_prefs"

    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, unique=True, nullable=False)
    lang_code = Column(String(20),  default="en")
    lang_name = Column(String(100), default="English")
    updated_at= Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
