"""
eduai/app/services/auth_service.py
JWT + bcrypt authentication helpers for EduAI.
"""

import os
import hashlib
import secrets
import json
from datetime import datetime, timedelta
from typing import Optional

# ── Simple token store (file-backed so it survives restarts) ─────────────────
_TOKENS_FILE = os.path.join(os.path.dirname(__file__), "../../.eduai_tokens.json")

def _load_tokens() -> dict:
    try:
        with open(_TOKENS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_tokens(tokens: dict):
    try:
        os.makedirs(os.path.dirname(_TOKENS_FILE), exist_ok=True)
        with open(_TOKENS_FILE, "w") as f:
            json.dump(tokens, f, default=str)
    except Exception as e:
        print(f"[EDUAI-AUTH] Token save failed: {e}")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


def create_token(user_id: int, role: str) -> str:
    token   = secrets.token_hex(32)
    tokens  = _load_tokens()
    tokens[token] = {
        "user_id":  user_id,
        "role":     role,
        "created":  datetime.utcnow().isoformat(),
        "expires":  (datetime.utcnow() + timedelta(days=30)).isoformat(),
    }
    _save_tokens(tokens)
    return token


def verify_token(token: str) -> Optional[dict]:
    """Returns {user_id, role} or None if invalid/expired."""
    tokens = _load_tokens()
    data   = tokens.get(token)
    if not data:
        return None
    try:
        expires = datetime.fromisoformat(data["expires"])
        if datetime.utcnow() > expires:
            return None
    except Exception:
        return None
    return {"user_id": data["user_id"], "role": data["role"]}


def revoke_token(token: str):
    tokens = _load_tokens()
    tokens.pop(token, None)
    _save_tokens(tokens)