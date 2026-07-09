"""
eduai/app/battle_models.py
Place this file at:  eduai/app/battle_models.py
(Same folder as models.py, community_models.py, streak_models.py)

Battle Mode (1v1 Quiz Race) — polling-based, no WebSocket dependency.

How it works:
  1. Player A creates a battle (status='waiting') with an invite code.
  2. Player B joins via the code (status='active').
  3. Both players poll GET /battle/<id>/state every ~2s.
  4. Each answer submission updates BattleAnswer + recalculates progress.
  5. First to finish all questions (or time runs out) → status='finished'.

This is intentionally simple (no Redis/pubsub) — fine for a handful of
concurrent battles on SQLite. If you outgrow this, swap the polling loop
for flask-socketio without changing the data model.
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
from eduai.app.database import Base


class Battle(Base):
    __tablename__ = 'eduai_battles'

    id              = Column(Integer, primary_key=True, index=True)
    invite_code     = Column(String(10), unique=True, nullable=False, index=True)
    subject         = Column(String(100), nullable=True)
    class_level     = Column(String(20), nullable=True)
    difficulty      = Column(String(20), default='Medium')
    questions_json  = Column(Text, nullable=False)   # same shape as quiz questions

    player1_id      = Column(Integer, ForeignKey('eduai_users.id'), nullable=False)
    player1_name    = Column(String(120), nullable=False)
    player2_id      = Column(Integer, ForeignKey('eduai_users.id'), nullable=True)
    player2_name    = Column(String(120), nullable=True)

    status          = Column(String(20), default='waiting')  # waiting | active | finished | expired
    winner_id       = Column(Integer, nullable=True)          # null = draw

    created_at      = Column(DateTime, default=datetime.utcnow)
    started_at      = Column(DateTime, nullable=True)
    finished_at     = Column(DateTime, nullable=True)


class BattleAnswer(Base):
    """One row per (battle, player, question). Used to compute progress + score."""
    __tablename__ = 'eduai_battle_answers'

    id            = Column(Integer, primary_key=True, index=True)
    battle_id     = Column(Integer, ForeignKey('eduai_battles.id'), nullable=False, index=True)
    player_id     = Column(Integer, nullable=False, index=True)
    question_index= Column(Integer, nullable=False)
    chosen_index  = Column(Integer, nullable=True)   # 0-based option index
    is_correct    = Column(Boolean, default=False)
    answered_at   = Column(DateTime, default=datetime.utcnow)


class TextbookScan(Base):
    """Stores results of textbook photo scans for the learner's history."""
    __tablename__ = 'eduai_textbook_scans'

    id            = Column(Integer, primary_key=True, index=True)
    learner_id    = Column(Integer, ForeignKey('eduai_users.id'), nullable=False, index=True)
    subject       = Column(String(100), nullable=True)
    extracted_text= Column(Text, nullable=True)
    explanation   = Column(Text, nullable=True)
    flashcards_json=Column(Text, nullable=True)   # [{front, back}]
    quiz_json     = Column(Text, nullable=True)   # generated quiz questions
    created_at    = Column(DateTime, default=datetime.utcnow)