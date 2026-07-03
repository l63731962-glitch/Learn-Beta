"""
eduai/app/database.py
SQLAlchemy database engine and session setup for EduAI.
Uses SQLite by default (no extra server needed).
Point DATABASE_URL at Postgres/MySQL in .env for production.

CHANGED from original:
  - init_db() now also imports community_models so the community tables
    (community_messages, community_resources, etc.) are created on startup.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Default: SQLite file alongside your project.
# Override with DATABASE_URL=postgresql://user:pass@host/dbname in .env
DATABASE_URL = os.getenv(
    "EDUAI_DATABASE_URL",
    os.getenv("DATABASE_URL", "sqlite:///./eduai.db")
)

# SQLite needs check_same_thread=False; ignored for other backends
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,         # set to True for SQL debug logging
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI-style dependency — yields a DB session, always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_direct():
    """Synchronous helper for non-FastAPI code (used in Flask routes)."""
    return SessionLocal()


def init_db():
    """Create all tables. Safe to call multiple times (CREATE TABLE IF NOT EXISTS)."""
    from eduai.app import models              # noqa: F401 — EduAI core tables
    from eduai.app import community_models    # noqa: F401 — Community tables
    from eduai.app import streak_models       # noqa: F401 — Streaks/Challenges/Parent tables
    from eduai.app import battle_models       # noqa: F401 — Battle Mode + Textbook Scanner tables
    from eduai.app import phase3_models       # noqa: F401 — Curriculum gap + exam readiness tables
    from eduai.app import community_models    # noqa: F401 — ensures AppRating table is created
    Base.metadata.create_all(bind=engine)
    print("[EDUAI-DB] ✅ Tables created / verified (core + community + streaks + battle + phase3)")

    # ── Safe migration: add org_id to community tables if not present ────────
    _community_tables = [
        'community_messages',
        'community_resources',
        'community_announcements',
        'group_test_assignments',
        'homework_questions',
    ]
    try:
        with engine.connect() as conn:
            for table in _community_tables:
                if DATABASE_URL.startswith("sqlite"):
                    result = conn.execute(
                        __import__("sqlalchemy").text(f"PRAGMA table_info({table})")
                    ).fetchall()
                    col_names = [row[1] for row in result]
                    if result and "org_id" not in col_names:
                        conn.execute(
                            __import__("sqlalchemy").text(
                                f"ALTER TABLE {table} ADD COLUMN org_id INTEGER REFERENCES eduai_organizations(id)"
                            )
                        )
                        conn.commit()
                        print(f"[EDUAI-DB] ✅ Migration: added org_id to {table}")
                else:
                    result = conn.execute(
                        __import__("sqlalchemy").text(
                            f"SELECT column_name FROM information_schema.columns "
                            f"WHERE table_name='{table}' AND column_name='org_id'"
                        )
                    ).fetchone()
                    if not result:
                        conn.execute(
                            __import__("sqlalchemy").text(
                                f"ALTER TABLE {table} ADD COLUMN org_id INTEGER"
                            )
                        )
                        conn.commit()
                        print(f"[EDUAI-DB] ✅ Migration: added org_id to {table}")
    except Exception as e:
        print(f"[EDUAI-DB] Community migration note: {e}")
    # This runs every startup but is a no-op if the column already exists.
    try:
        with engine.connect() as conn:
            if DATABASE_URL.startswith("sqlite"):
                # SQLite: check pragma for column existence
                result = conn.execute(
                    __import__("sqlalchemy").text("PRAGMA table_info(eduai_leaderboard)")
                ).fetchall()
                col_names = [row[1] for row in result]
                if "org_id" not in col_names:
                    conn.execute(
                        __import__("sqlalchemy").text(
                            "ALTER TABLE eduai_leaderboard ADD COLUMN org_id INTEGER REFERENCES eduai_organizations(id)"
                        )
                    )
                    conn.commit()
                    print("[EDUAI-DB] ✅ Migration: added org_id to eduai_leaderboard")
            else:
                # PostgreSQL / MySQL: use information_schema
                result = conn.execute(
                    __import__("sqlalchemy").text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='eduai_leaderboard' AND column_name='org_id'"
                    )
                ).fetchone()
                if not result:
                    conn.execute(
                        __import__("sqlalchemy").text(
                            "ALTER TABLE eduai_leaderboard ADD COLUMN org_id INTEGER"
                        )
                    )
                    conn.commit()
                    print("[EDUAI-DB] ✅ Migration: added org_id to eduai_leaderboard")
    except Exception as e:
        print(f"[EDUAI-DB] Migration note: {e}")

    # ── Safe migration: add subscription columns to eduai_users ──────────
    _user_sub_columns = {
        "subscription_status":     "VARCHAR(20) DEFAULT 'trial'",
        "trial_started_at":        "DATETIME",
        "trial_ends_at":           "DATETIME",
        "paypal_subscription_id":  "VARCHAR(100)",
        "paypal_plan_id":          "VARCHAR(100)",
        "subscription_updated_at": "DATETIME",
    }
    try:
        with engine.connect() as conn:
            if DATABASE_URL.startswith("sqlite"):
                result = conn.execute(
                    __import__("sqlalchemy").text("PRAGMA table_info(eduai_users)")
                ).fetchall()
                existing_cols = [row[1] for row in result]
                for col_name, col_def in _user_sub_columns.items():
                    if col_name not in existing_cols:
                        conn.execute(
                            __import__("sqlalchemy").text(
                                f"ALTER TABLE eduai_users ADD COLUMN {col_name} {col_def}"
                            )
                        )
                        conn.commit()
                        print(f"[EDUAI-DB] ✅ Migration: added {col_name} to eduai_users")
    except Exception as e:
        print(f"[EDUAI-DB] Subscription migration note: {e}")
