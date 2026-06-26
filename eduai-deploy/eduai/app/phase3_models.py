"""
eduai/app/phase3_models.py
Place this file at:  eduai/app/phase3_models.py
(Same folder as models.py, streak_models.py, battle_models.py)

Phase 3 — Chunk A: Curriculum Gap Detector + Exam Readiness Score.
Both are read-only analyses over existing QuizResult/TestResult data,
cached here so learners don't regenerate the AI report on every page load.

database.py needs ONE import line added (see integration guide).
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
from eduai.app.database import Base


class CurriculumGapReport(Base):
    """Cached AI analysis of a learner's weak topics/subjects, regenerated
    on demand (e.g. once per day or on explicit refresh)."""
    __tablename__ = 'eduai_curriculum_gap_reports'

    id            = Column(Integer, primary_key=True, index=True)
    learner_id    = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    report_date   = Column(String(10), nullable=False, index=True)  # 'YYYY-MM-DD' UTC
    gaps_json     = Column(Text, nullable=False)   # [{subject, topic, severity, recommendation}]
    summary_text  = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


class ExamReadinessScore(Base):
    """Cached AI-generated exam readiness assessment for a target exam
    (e.g. WAEC, JAMB, NECO) and subject."""
    __tablename__ = 'eduai_exam_readiness_scores'

    id              = Column(Integer, primary_key=True, index=True)
    learner_id      = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    exam_type       = Column(String(50),  nullable=False)   # 'WAEC' | 'JAMB' | 'NECO' | etc.
    subject         = Column(String(100), nullable=False)
    readiness_pct   = Column(Float, nullable=False)         # 0-100
    breakdown_json  = Column(Text, nullable=True)           # {topic: pct}
    recommendation  = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)


class ReportCard(Base):
    """AI-generated narrative report card for a learner, covering a date
    range. Teachers can generate these for any learner; learners can
    generate their own."""
    __tablename__ = 'eduai_report_cards'

    id              = Column(Integer, primary_key=True, index=True)
    learner_id      = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    generated_by_id = Column(Integer, ForeignKey('eduai_users.id'), nullable=True)  # teacher_id, or null if self
    period_start    = Column(String(10), nullable=False)   # 'YYYY-MM-DD'
    period_end      = Column(String(10), nullable=False)   # 'YYYY-MM-DD'
    overall_grade   = Column(String(5),  nullable=True)    # 'A+', 'B', etc.
    content_md      = Column(Text, nullable=False)         # full markdown report card
    stats_json       = Column(Text, nullable=True)         # {avg_score, quizzes_done, subjects: {...}}
    created_at      = Column(DateTime, default=datetime.utcnow)


class EssayCheck(Base):
    """Stores an essay/assignment submission and the AI's feedback."""
    __tablename__ = 'eduai_essay_checks'

    id              = Column(Integer, primary_key=True, index=True)
    learner_id      = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    subject         = Column(String(100), nullable=True)
    title           = Column(String(300), nullable=True)
    essay_text      = Column(Text, nullable=False)
    feedback_json   = Column(Text, nullable=False)   # {overall_score, strengths, weaknesses, suggestions, annotated_text}
    created_at      = Column(DateTime, default=datetime.utcnow)


class VideoLessonPlan(Base):
    """AI-generated lesson plan derived from a YouTube video's transcript."""
    __tablename__ = 'eduai_video_lesson_plans'

    id            = Column(Integer, primary_key=True, index=True)
    teacher_id    = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    video_url     = Column(String(500), nullable=False)
    video_id      = Column(String(30),  nullable=False, index=True)
    video_title   = Column(String(500), nullable=True)   # best-effort, may be null
    subject       = Column(String(100), nullable=True)
    class_level   = Column(String(20),  nullable=True)
    transcript_lang = Column(String(10), nullable=True)
    content_md    = Column(Text, nullable=False)         # full lesson plan markdown
    created_at    = Column(DateTime, default=datetime.utcnow)