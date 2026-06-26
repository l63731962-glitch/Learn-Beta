"""
eduai/app/routes/i18n_routes.py
─────────────────────────────────────────────────────────────────────────────
Centralized UI-translation service for AIR-EduAI.

Why this exists:
    Previously, every UI string for every language lived in one giant
    JS object (`UI_TRANSLATIONS`) inside learn.html, hand-written for
    only 9 of the 100+ languages offered in the language picker.
    Adding a string meant editing 9 places by hand, and the other ~90
    languages silently fell back to English.

How it works now:
    - English strings live in eduai/app/translations/en.json — this is
      the SOURCE OF TRUTH. To add a new UI string, add ONE key here.
    - Every other language gets its own JSON cache file in the same
      folder (es.json, fr.json, hi.json, de.json, ...).
    - GET /eduai/i18n/<lang_code> returns the full dict for that
      language. If the cache file doesn't exist yet, or is missing keys
      present in en.json, those keys are auto-translated via the AI
      service (Claude/OpenAI — same provider used elsewhere in EduAI)
      and written back to the cache file so the next request is instant.
    - en (English) is always served directly from en.json with no AI
      calls — it's the reference copy.

Frontend usage (learn.html):
    const dict = await fetch(`/eduai/i18n/${langCode}`).then(r=>r.json());
    function t(key){ return dict[key.lang] || dict.dict[key] || key; }
    (see learn.html for the exact small loader — this just serves data)

Routes:
    GET  /eduai/i18n/<lang_code>          → { lang, dict: {key: text, ...} }
    POST /eduai/i18n/translate-missing    → { lang, added: [...keys] }
                                              body: { lang_code, lang_name }
                                              Pre-warms the cache for one
                                              language (useful right after
                                              a user picks a new language,
                                              so the FIRST page they see
                                              isn't waiting on AI calls).
    POST /eduai/i18n/add-key              → adds a new key to en.json and
                                              clears it from every other
                                              cached language file, so it
                                              gets (re)translated on next
                                              fetch. Admin/dev use.
                                              body: { key, en_text }
"""

import os
import json
import threading
from flask import Blueprint, request, jsonify

from eduai.app.service import ai_service

# ── Blueprint ──────────────────────────────────────────────────────────────
i18n_bp = Blueprint("eduai_i18n", __name__, url_prefix="/eduai/i18n")

# ── Paths ──────────────────────────────────────────────────────────────────
_TRANSLATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "translations")
os.makedirs(_TRANSLATIONS_DIR, exist_ok=True)

_EN_FILE = os.path.join(_TRANSLATIONS_DIR, "en.json")

# One lock per language file to avoid two requests racing to translate +
# write the same file at the same time (e.g. two tabs open at once).
_locks = {}
_locks_guard = threading.Lock()


def _lock_for(lang_code: str) -> threading.Lock:
    with _locks_guard:
        if lang_code not in _locks:
            _locks[lang_code] = threading.Lock()
        return _locks[lang_code]


# ════════════════════════════════════════════════════════════════════════════
# FILE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _safe_lang_code(lang_code: str) -> str:
    """Prevent path traversal — only allow simple language-code-like strings."""
    code = (lang_code or "en").strip().lower()
    # Language codes look like 'en', 'es', 'zh-tw', 'pt-br' — letters/digits/hyphen only
    cleaned = "".join(c for c in code if c.isalnum() or c == "-")
    return cleaned[:10] or "en"


def _file_for(lang_code: str) -> str:
    return os.path.join(_TRANSLATIONS_DIR, f"{_safe_lang_code(lang_code)}.json")


def _load_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[I18N] Failed to read {path}: {e}")
        return {}


def _save_json(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as e:
        print(f"[I18N] Failed to write {path}: {e}")


def load_english() -> dict:
    """The source-of-truth dict. Every other language is measured against this."""
    en = _load_json(_EN_FILE)
    if not en:
        print(f"[I18N] ⚠️  en.json missing or empty at {_EN_FILE}")
    return en


# ════════════════════════════════════════════════════════════════════════════
# AI TRANSLATION
# ════════════════════════════════════════════════════════════════════════════

# Friendly names help the AI pick the right register/dialect.
# Falls back to the raw code if not listed — still works fine.
_LANG_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "ar": "Arabic", "zh": "Chinese (Simplified)", "zh-tw": "Chinese (Traditional)",
    "yo": "Yoruba", "ig": "Igbo", "ha": "Hausa", "hi": "Hindi", "bn": "Bengali",
    "de": "German", "ru": "Russian", "ja": "Japanese", "ko": "Korean",
    "it": "Italian", "tr": "Turkish", "nl": "Dutch", "pl": "Polish",
    "sw": "Swahili", "am": "Amharic", "zu": "Zulu", "af": "Afrikaans",
    "ur": "Urdu", "fa": "Persian/Farsi", "he": "Hebrew", "ta": "Tamil",
    "te": "Telugu", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian",
    "ms": "Malay",
}


def _lang_display_name(lang_code: str, lang_name: str = None) -> str:
    if lang_name:
        return lang_name
    return _LANG_NAMES.get(lang_code, lang_code)


def _translate_batch(keys_and_text: dict, target_lang_code: str, target_lang_name: str = None) -> dict:
    """
    Translate a batch of {key: english_text} pairs into the target language
    using the same AI provider EduAI already uses elsewhere.
    Returns {key: translated_text}. On any failure, returns {} so the
    caller can fall back to English for those keys without crashing.
    """
    if not keys_and_text:
        return {}

    display_name = _lang_display_name(target_lang_code, target_lang_name)

    # Send as a JSON object so the model returns a JSON object back —
    # keeps keys aligned 1:1 and avoids ordering/splitting issues.
    source_json = json.dumps(keys_and_text, ensure_ascii=False, indent=2)

    system = (
        "You are a professional UI localization translator for an education "
        "app used by teachers and students worldwide. Translate the VALUES "
        "of the given JSON object from English into "
        f"{display_name}. Keep the JSON KEYS exactly unchanged. "
        "Keep emoji, placeholders like {name}, and punctuation. "
        "Use natural, concise UI wording appropriate for buttons/labels "
        "(not literal word-for-word translation). "
        "Respond with ONLY the translated JSON object — no markdown, "
        "no code fences, no commentary, no explanation."
    )

    messages = [{"role": "user", "content": source_json}]

    try:
        raw = ai_service._ai_call(messages, system, max_tokens=4000)
        if not raw:
            return {}

        # Strip accidental code fences if the model adds them anyway
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()

        translated = json.loads(cleaned)
        if not isinstance(translated, dict):
            return {}

        # Only keep keys we actually asked for, and only string values
        result = {}
        for k in keys_and_text:
            v = translated.get(k)
            if isinstance(v, str) and v.strip():
                result[k] = v
        return result

    except Exception as e:
        print(f"[I18N] AI translation failed for {target_lang_code}: {e}")
        return {}


# ════════════════════════════════════════════════════════════════════════════
# CORE: GET FULL DICT FOR A LANGUAGE (with auto-fill of missing keys)
# ════════════════════════════════════════════════════════════════════════════

def get_translations(lang_code: str, lang_name: str = None) -> dict:
    """
    Returns the complete {key: text} dict for lang_code.
    - For 'en', returns en.json as-is (no AI calls).
    - For any other language, returns the cached file, auto-translating
      and persisting any keys that exist in en.json but not yet in the
      cache (new strings added to the app, or first-ever request for
      this language).
    """
    lang_code = _safe_lang_code(lang_code)
    en = load_english()

    if lang_code == "en":
        return en

    cache_path = _file_for(lang_code)

    with _lock_for(lang_code):
        cached = _load_json(cache_path)
        missing = {k: v for k, v in en.items() if k not in cached}

        if missing:
            print(f"[I18N] {lang_code}: translating {len(missing)} missing key(s)")
            newly_translated = _translate_batch(missing, lang_code, lang_name)
            if newly_translated:
                cached.update(newly_translated)
                _save_json(cache_path, cached)

        # Final fallback: anything still missing (AI call failed, etc.)
        # falls back to English so the UI never shows raw key names.
        merged = dict(en)
        merged.update(cached)
        return merged


# ════════════════════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════════════════════

@i18n_bp.route("/<lang_code>", methods=["GET"])
def get_language(lang_code):
    """
    GET /eduai/i18n/<lang_code>
    Optional query param: ?lang_name=Hindi  (improves AI translation quality
    for the first request in a language that isn't in _LANG_NAMES)

    Returns:
        { "lang": "es", "dict": { "welcome_back": "Bienvenido", ... } }
    """
    lang_name = request.args.get("lang_name")
    try:
        dict_ = get_translations(lang_code, lang_name)
        return jsonify({"lang": _safe_lang_code(lang_code), "dict": dict_}), 200
    except Exception as e:
        # Never break the UI — fall back to English on any server error.
        print(f"[I18N] Error serving {lang_code}: {e}")
        return jsonify({"lang": "en", "dict": load_english()}), 200


@i18n_bp.route("/translate-missing", methods=["POST"])
def translate_missing():
    """
    POST /eduai/i18n/translate-missing
    Body: { "lang_code": "hi", "lang_name": "Hindi" }

    Pre-warms the cache for one language so the user's first page load
    after switching languages doesn't wait on AI calls. Safe to call
    even if the cache is already complete (no-op in that case).
    """
    data = request.get_json(force=True) or {}
    lang_code = data.get("lang_code", "")
    lang_name = data.get("lang_name")

    if not lang_code:
        return jsonify({"error": "lang_code is required"}), 400

    lang_code = _safe_lang_code(lang_code)
    en = load_english()

    if lang_code == "en":
        return jsonify({"lang": "en", "added": []}), 200

    cache_path = _file_for(lang_code)

    with _lock_for(lang_code):
        cached = _load_json(cache_path)
        missing = {k: v for k, v in en.items() if k not in cached}

        added = []
        if missing:
            newly_translated = _translate_batch(missing, lang_code, lang_name)
            if newly_translated:
                cached.update(newly_translated)
                _save_json(cache_path, cached)
                added = list(newly_translated.keys())

    return jsonify({"lang": lang_code, "added": added, "total_keys": len(en)}), 200


@i18n_bp.route("/add-key", methods=["POST"])
def add_key():
    """
    POST /eduai/i18n/add-key
    Body: { "key": "new_button_label", "en_text": "Export to PDF" }

    Adds (or updates) one key in en.json — the source of truth.
    Does NOT translate other languages immediately; they'll pick up the
    new key automatically the next time get_translations() runs for them
    (lazy, on next page load). Use /translate-missing afterwards for any
    language you want pre-warmed right away.

    Intended for use by developers/admin tooling, not end users.
    """
    data = request.get_json(force=True) or {}
    key = (data.get("key") or "").strip()
    en_text = data.get("en_text")

    if not key or en_text is None:
        return jsonify({"error": "key and en_text are required"}), 400

    with _lock_for("en"):
        en = load_english()
        en[key] = en_text
        _save_json(_EN_FILE, en)

    return jsonify({"status": "added", "key": key}), 200


@i18n_bp.route("/languages", methods=["GET"])
def list_cached_languages():
    """
    GET /eduai/i18n/languages
    Returns which languages already have a cache file (i.e. have been
    requested at least once) and how complete each is vs en.json.
    Useful for an admin dashboard.
    """
    en = load_english()
    total = len(en)
    out = []
    try:
        for fname in sorted(os.listdir(_TRANSLATIONS_DIR)):
            if not fname.endswith(".json"):
                continue
            code = fname[:-5]
            cached = _load_json(os.path.join(_TRANSLATIONS_DIR, fname))
            covered = sum(1 for k in en if k in cached)
            out.append({
                "lang": code,
                "keys_translated": covered,
                "keys_total": total,
                "complete": covered >= total,
            })
    except Exception as e:
        print(f"[I18N] languages listing error: {e}")

    return jsonify({"languages": out, "total_keys": total}), 200


# ════════════════════════════════════════════════════════════════════════════
# REGISTRATION HELPER
# ════════════════════════════════════════════════════════════════════════════

def register_i18n_routes(app):
    """
    Attach this blueprint to the OMEGA Flask app.

    Usage in api.py:

        try:
            from eduai.app.routes.i18n_routes import register_i18n_routes
            register_i18n_routes(app)
            print("[API] ✅ EduAI i18n routes registered at /eduai/i18n")
        except Exception as e:
            print(f"[API] i18n routes skipped: {e}")
    """
    app.register_blueprint(i18n_bp)
    print("[EDUAI-I18N] ✅ i18n routes registered at /eduai/i18n")