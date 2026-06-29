"""
eduai/app/services/ai_service.py
─────────────────────────────────────────────────────────────────────────────
ALL AI calls for EduAI routed through this module.

Both provider options in the UI toggle ("anthropic" / "openai") now go
through a SINGLE backend: OpenRouter (https://openrouter.ai). One API key,
one endpoint. The toggle still controls which underlying model gets used —
"anthropic" → a Claude model served via OpenRouter, "openai" → a GPT model
served via OpenRouter. No direct calls to api.anthropic.com or the OpenAI
SDK/client remain.

Supports:
  • Lesson note generation (streaming + non-streaming)
  • Test / CBT question generation
  • Learner quiz generation
  • "Learn anything" explanation
  • Scheme of Work topic extraction
  • AI Tutor chat
  • Scheme bulk note generation
"""

import os
import re
import json
from typing import Generator, Optional

# ── OpenRouter config ─────────────────────────────────────────────────────
# Single key, single endpoint for both toggle options.
# Set OPENROUTER_API_KEY in your .env (replaces ANTHROPIC_API_KEY / OPENAI_API_KEY).
OPENROUTER_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL  = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_OK  = bool(OPENROUTER_KEY)

# Optional headers OpenRouter uses for its public leaderboard / rankings.
# Harmless to leave as-is; change to your real domain/app name if you like.
OPENROUTER_SITE_URL  = os.getenv("OPENROUTER_SITE_URL", "https://learn-beta-ftw5qr.appdeploy.ai")
OPENROUTER_SITE_NAME = os.getenv("OPENROUTER_SITE_NAME", "Learn-Beta")

# Model used when the toggle is set to each provider value.
# These are OpenRouter "vendor/model" slugs — change freely without touching
# any call site below.
PROVIDER_MODELS = {
    "anthropic": os.getenv("OPENROUTER_CLAUDE_MODEL", "anthropic/claude-sonnet-4"),
    "openai":    os.getenv("OPENROUTER_OPENAI_MODEL", "openai/gpt-4o"),
}
DEFAULT_PROVIDER = "openai"

# Backward-compat flags some older code in this file may still reference.
_ANTHROPIC_OK = _OPENROUTER_OK
_OPENAI_OK    = _OPENROUTER_OK

# ── Age / class calibration strings ─────────────────────────────────────────
_AGE_GUIDES = {
    "kg": (
        "Use very simple words, rhymes, fun animal stories, and emoji. "
        "Max 2 sentences per point. Make it magical and playful for 4–6-year-olds."
    ),
    "primary": (
        "Use everyday examples from home and school. Short clear paragraphs. "
        "Analogies to toys, food, games. Friendly, encouraging, step-by-step."
    ),
    "jss": (
        "Clear detailed explanation with real-world applications. "
        "Include exam tips. Relatable teenage examples. Build confidence."
    ),
    "sss": (
        "Detailed academic explanation. Include exam strategies and connections "
        "to other subjects. Critical thinking prompts. Exam-focused."
    ),
    "university": (
        "Academic depth with research context. Reference key theories. "
        "Encourage critical analysis and deeper inquiry."
    ),
}


def _resolve_model(provider: Optional[str]) -> str:
    """Map a toggle value ('anthropic' | 'openai') to an OpenRouter model slug."""
    key = (provider or DEFAULT_PROVIDER).lower()
    return PROVIDER_MODELS.get(key, PROVIDER_MODELS[DEFAULT_PROVIDER])


def _openrouter_headers() -> dict:
    return {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        # Optional but recommended by OpenRouter for attribution/rankings.
        "HTTP-Referer":  OPENROUTER_SITE_URL,
        "X-Title":       OPENROUTER_SITE_NAME,
    }


def _openrouter_chat(
    messages: list,
    system: str,
    max_tokens: int = 4000,
    provider: Optional[str] = None,
) -> Optional[str]:
    """Non-streaming call via OpenRouter. `provider` picks the underlying model."""
    import requests
    if not _OPENROUTER_OK:
        return None
    model = _resolve_model(provider)
    try:
        full_messages = [{"role": "system", "content": system}] + messages
        r = requests.post(
            OPENROUTER_URL,
            headers=_openrouter_headers(),
            json={
                "model":      model,
                "max_tokens": max_tokens,
                "messages":   full_messages,
            },
            timeout=120,
        )
        if r.ok:
            return r.json()["choices"][0]["message"]["content"].strip()
        print(f"[EDUAI-AI] OpenRouter error {r.status_code} ({model}): {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[EDUAI-AI] OpenRouter call failed ({model}): {e}")
        return None


def _ai_call(
    messages: list,
    system: str,
    max_tokens: int = 4000,
    provider: Optional[str] = None,
) -> Optional[str]:
    """
    Single entry point for non-streaming generation.
    `provider` ('anthropic' | 'openai') selects the model via OpenRouter;
    defaults to DEFAULT_PROVIDER if not given so existing callers keep working.
    """
    return _openrouter_chat(messages, system, max_tokens, provider=provider)


def _openrouter_stream(
    messages: list,
    system: str,
    max_tokens: int = 4000,
    provider: Optional[str] = None,
) -> Generator[str, None, None]:
    """Streaming generator via OpenRouter — yields text deltas."""
    import requests
    if not _OPENROUTER_OK:
        return
    model = _resolve_model(provider)
    try:
        full_messages = [{"role": "system", "content": system}] + messages
        r = requests.post(
            OPENROUTER_URL,
            headers=_openrouter_headers(),
            json={
                "model":      model,
                "max_tokens": max_tokens,
                "messages":   full_messages,
                "stream":     True,
            },
            stream=True,
            timeout=120,
        )
        if not r.ok:
            print(f"[EDUAI-AI] OpenRouter stream error {r.status_code} ({model}): {r.text[:200]}")
            return
        for line in r.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8") if isinstance(line, bytes) else line
            if text.startswith("data: "):
                payload = text[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    d = json.loads(payload)
                    delta = d.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        yield delta
                except Exception:
                    pass
    except Exception as e:
        print(f"[EDUAI-AI] OpenRouter stream failed ({model}): {e}")


# Back-compat alias: existing call sites in this file use `_anthropic_stream(...)`.
# Keeping the name avoids touching every call site below; behavior now routes
# through OpenRouter using DEFAULT_PROVIDER unless a provider is passed through.
def _anthropic_stream(
    messages: list,
    system: str,
    max_tokens: int = 4000,
    provider: Optional[str] = None,
) -> Generator[str, None, None]:
    yield from _openrouter_stream(messages, system, max_tokens, provider=provider)


# ════════════════════════════════════════════════════════════════════════════
# LESSON NOTE GENERATION
# ════════════════════════════════════════════════════════════════════════════

def generate_lesson_note(
    subject:    str,
    class_level:str,
    sub_class:  str,
    topic:      str,
    duration:   str,
    curriculum: str,
    language:   str,
    stream:     bool = False,
    provider:   str = None,
):
    """
    Generate a complete, structured lesson note.
    If stream=True, returns a generator of text deltas.
    If stream=False, returns the full string (or None on failure).
    """
    class_label = sub_class or class_level

    system = (
        f"You are an expert educational content creator and master pedagogue. "
        f"Generate ALL content in {language} — write naturally and idiomatically. "
        f"Your lesson notes must be EXCEPTIONALLY detailed and structured so that "
        f"any teacher can pick it up and teach immediately without extra preparation."
    )

    prompt = f"""Create a complete, professional lesson note:
- Subject: {subject}
- Class Level: {class_label}
- Topic: {topic}
- Duration: {duration}
- Curriculum: {curriculum}
- Output Language: {language}

Use this EXACT structure with markdown headers:

# 📘 LESSON NOTE: {topic}

## 📋 LESSON INFORMATION
Subject: {subject} | Class: {class_label} | Duration: {duration} | Curriculum: {curriculum}

## 🎯 LEARNING OBJECTIVES
(6 specific, measurable objectives using Bloom's taxonomy verbs)

## 📖 KEY VOCABULARY
(12 key terms with clear, age-appropriate definitions)

## 🛠️ MATERIALS & RESOURCES NEEDED
(Complete list of teaching aids)

## 🚀 INTRODUCTION / HOOK
(An engaging story, question, or demonstration that activates prior knowledge)

## 📚 MAIN LESSON CONTENT

### Part 1: [First Core Concept]
(Detailed step-by-step explanation)

### Part 2: [Second Core Concept]
(Building on Part 1)

### Part 3: [Third Core Concept]
(Deepening understanding)

## ✏️ WORKED EXAMPLES
(4 detailed examples showing the complete thinking process)

## ⚠️ COMMON MISCONCEPTIONS & CORRECTIONS
(5 misconceptions students make and how to correct each)

## 🌟 DIFFERENTIATION STRATEGIES
### For Learners Who Need Support:
(3 specific support strategies)
### For Advanced Learners:
(3 extension challenges)

## 🎮 CLASSROOM ACTIVITIES
(3 interactive activities with clear instructions and time allocation)

## ✅ ASSESSMENT QUESTIONS
(10 questions — mix of recall, comprehension, application — with answers)

## 📝 LESSON SUMMARY & KEY TAKEAWAYS
(Concise bulleted summary)

## 🏠 HOMEWORK / ASSIGNMENT
(A meaningful homework task)

## 💡 TEACHER'S NOTES & TIPS
(Alternative explanations, cultural connections, common pitfalls)

Write EVERYTHING in {language}. Be thorough — this must be the best lesson note ever written for this topic."""

    messages = [{"role": "user", "content": prompt}]

    if stream:
        return _anthropic_stream(messages, system, max_tokens=4000, provider=provider)
    else:
        return _ai_call(messages, system, max_tokens=4000, provider=provider)


# ════════════════════════════════════════════════════════════════════════════
# TEST GENERATION
# ════════════════════════════════════════════════════════════════════════════

def generate_test_questions(
    subject:     str,
    class_level: str,
    sub_class:   str,
    topic:       str,
    num_q:       int,
    difficulty:  str,
    question_types: list,
    language:    str,
    provider:    str = None,
) -> Optional[str]:
    """Generate raw test questions as formatted text. Returns full AI output."""

    class_label = sub_class or class_level
    types_str   = ", ".join(question_types) if question_types else "Multiple Choice (4 options A B C D)"

    system = (
        f"You are an expert examiner. ALL content must be in {language} — "
        f"write naturally and fluently in {language}."
    )

    prompt = f"""Create a {difficulty} difficulty test with exactly {num_q} questions:
- Subject: {subject}
- Class: {class_label}
- Topic: {topic or "Full Term Coverage"}
- Question Types: {types_str}

Format EVERY question EXACTLY like this — no deviation:

Q1. [Question text in {language}]
A) Option one
B) Option two
C) *Option three (CORRECT — mark with asterisk before the option text)
D) Option four
Answer: C
Explanation: [One sentence why this is correct]

Generate all {num_q} questions this way.
For True/False, use only A) True  B) False  Answer: A or B.
For Fill-in-the-blank, write the blank as _____
For Short Answer, omit options and just give Answer: [expected response]

Write EVERYTHING in {language}."""

    return _ai_call(
        [{"role": "user", "content": prompt}],
        system,
        max_tokens=min(4000, num_q * 150 + 500),
        provider=provider,
    )


def parse_test_questions(raw_text: str, max_q: int = 100) -> list:
    """
    Parse AI-generated test output into structured dicts.
    Returns list of {question, opts:[{letter,text,correct}], ans, exp}
    """
    questions = []
    blocks = re.split(r"\nQ\d+\.", raw_text)
    for block in blocks[1:]:
        if len(questions) >= max_q:
            break
        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
        if not lines:
            continue
        question = lines[0].strip()
        opts, ans, exp = [], "", ""
        for line in lines[1:]:
            if re.match(r"^[A-D]\)", line):
                correct = "*" in line
                letter  = line[0]
                text    = re.sub(r"^[A-D]\)\s*\*?", "", line).strip()
                if correct:
                    ans = letter
                opts.append({"letter": letter, "text": text, "correct": correct})
            elif line.lower().startswith("answer:"):
                candidate = line.replace("Answer:", "").replace("answer:", "").strip()
                if candidate and not ans:
                    ans = candidate[:2].strip()
            elif line.lower().startswith("explanation:"):
                exp = line.replace("Explanation:", "").replace("explanation:", "").strip()
        # Mark correct option if only Answer: was given
        if ans and not any(o["correct"] for o in opts):
            for o in opts:
                if o["letter"] == ans:
                    o["correct"] = True
                    break
        if question and opts:
            questions.append({"question": question, "opts": opts, "ans": ans, "exp": exp})
    return questions


# ════════════════════════════════════════════════════════════════════════════
# LEARNER — EXPLAIN TOPIC
# ════════════════════════════════════════════════════════════════════════════

def explain_topic(
    topic:       str,
    subject:     str,
    class_level: str,
    sub_class:   str,
    language:    str,
    stream:      bool = False,
    provider:    str = None,
):
    """
    Generate a learner-appropriate explanation of any topic.
    Adapts complexity and style to the class level.
    """
    age_guide   = _AGE_GUIDES.get(class_level, _AGE_GUIDES["primary"])
    class_label = sub_class or class_level

    system = (
        f"You are an exceptional AI teacher. Teach with clarity, passion, and deep understanding. "
        f"ALWAYS write in {language} — naturally and fluently. "
        f"Age guide: {age_guide}"
    )

    prompt = f"""Teach me about: "{topic}"{f" (subject: {subject})" if subject else ""}
My class: {class_label} | Language: {language}

Create an EXCEPTIONAL, engaging explanation:

## 🌟 Introduction
(A hook that makes me excited to learn — perfect for {class_label})

## 🎯 What You'll Understand After This
(3-4 clear, student-friendly learning goals)

## 📚 The Main Explanation
(Explain the core concept completely. Break into clear parts. Use examples appropriate for {class_label}.)

## 💡 Real Examples
(2-3 vivid, concrete examples that make it crystal clear)

## 🔍 Think About This
(One thought-provoking question or fascinating connection)

## ⭐ Amazing Fact
(One incredible fact about this topic that will stick in memory)

## ✅ Check Your Understanding
(3 questions the learner should be able to answer after reading)

## 📝 Summary
(5 key bullet points summarizing everything)

Write entirely in {language}. Make it so clear that a complete beginner understands and remembers it."""

    messages = [{"role": "user", "content": prompt}]
    if stream:
        return _anthropic_stream(messages, system, max_tokens=3000, provider=provider)
    return _ai_call(messages, system, max_tokens=3000, provider=provider)


# ════════════════════════════════════════════════════════════════════════════
# LEARNER — QUIZ GENERATION
# ════════════════════════════════════════════════════════════════════════════

def generate_quiz(
    subject:     str,
    topic:       str,
    class_level: str,
    sub_class:   str,
    num_q:       int,
    difficulty:  str,
    language:    str,
    provider:    str = None,
) -> Optional[list]:
    """
    Generate quiz questions as a parsed list of dicts.
    Returns [{"question":..., "options":[...], "correct":int, "explanation":...}]
    """
    class_label = sub_class or class_level

    system = (
        f"You are a quiz master. ALL content in {language}. "
        "Return ONLY a valid JSON array, no markdown, no explanation."
    )

    prompt = f"""Generate exactly {num_q} {difficulty} multiple-choice quiz questions for:
- Subject: {subject or "General Knowledge"}
- Topic: {topic or f"General {subject}"}
- Class: {class_label}

Return ONLY this JSON (no markdown, no extra text):
[
  {{
    "question": "Question text in {language}",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "correct": 0,
    "explanation": "Why this answer is correct"
  }}
]

"correct" is the 0-based index of the correct option (0=A, 1=B, 2=C, 3=D).
All text must be in {language}. Generate all {num_q} questions."""

    raw = _ai_call([{"role": "user", "content": prompt}], system, max_tokens=min(4000, num_q * 150), provider=provider)
    if not raw:
        return None

    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        if isinstance(data, list):
            return data[:num_q]
    except Exception as e:
        print(f"[EDUAI-AI] Quiz JSON parse failed: {e} — raw[:200]: {raw[:200]}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# AI TUTOR CHAT
# ════════════════════════════════════════════════════════════════════════════

_TUTOR_CHARS = {
    "kg":         ("🐻", "Buddy Bear"),
    "primary":    ("🦁", "Leo"),
    "jss":        ("🤖", "AI Rex"),
    "sss":        ("🎓", "Scholar"),
    "university": ("👨‍💼", "Prof. AI"),
}

def tutor_chat(
    message:     str,
    history:     list,
    class_level: str,
    sub_class:   str,
    language:    str,
    provider:    str = None,
) -> Optional[str]:
    """
    Respond as the class-appropriate AI tutor.
    history: list of {"role":"user"|"assistant", "content":"..."}
    """
    age_guide   = _AGE_GUIDES.get(class_level, _AGE_GUIDES["primary"])
    class_label = sub_class or class_level
    _, name     = _TUTOR_CHARS.get(class_level, ("🤖", "AI Tutor"))

    system = (
        f"You are {name}, a warm, encouraging AI tutor for a {class_label} student. "
        f"ALWAYS respond in {language} — naturally and fluently. "
        f"Age guide: {age_guide} "
        f"Be patient, supportive, never condescending. Celebrate progress. "
        f"If the student is confused, try a completely different explanation approach. "
        f"Keep responses helpful and focused — not too long."
    )

    messages = list(history[-20:])  # cap context at 20 turns
    messages.append({"role": "user", "content": message})

    return _ai_call(messages, system, max_tokens=1000, provider=provider)


# ════════════════════════════════════════════════════════════════════════════
# SCHEME OF WORK EXTRACTION
# ════════════════════════════════════════════════════════════════════════════

def extract_sow_topics(
    subject:     str,
    class_level: str,
    curriculum:  str,
    file_text:   str = "",
    provider:    str = None,
) -> Optional[list]:
    """
    Extract or generate a 12-week scheme of work.
    Returns [{week, topic, objectives}, ...]
    """
    system = (
        "You are a curriculum planner. "
        "Return ONLY valid JSON — no markdown, no preamble, no explanation."
    )

    if file_text.strip():
        prompt = (
            f"Extract a structured scheme of work from this document.\n"
            f"Subject: {subject}, Level: {class_level}, Curriculum: {curriculum}\n\n"
            f"Document content:\n{file_text[:6000]}\n\n"
            f"Return ONLY a JSON array:\n"
            f'[{{"week":1,"topic":"Topic Name","objectives":"Key objectives"}}]\n'
            f"If fewer than 12 weeks can be extracted, generate the remainder."
        )
    else:
        prompt = (
            f"Generate a 12-week scheme of work.\n"
            f"Subject: {subject}, Level: {class_level}, Curriculum: {curriculum}\n\n"
            f"Return ONLY a JSON array with exactly 12 objects:\n"
            f'[{{"week":1,"topic":"Topic Name","objectives":"Key learning objectives"}}]'
        )

    raw = _ai_call([{"role": "user", "content": prompt}], system, max_tokens=1500, provider=provider)
    if not raw:
        return None
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        if isinstance(data, list):
            return data[:20]  # max 20 weeks
    except Exception as e:
        print(f"[EDUAI-AI] SOW JSON parse failed: {e}")
    # Fallback: 12 generic topics
    return [
        {"week": i + 1, "topic": f"Week {i + 1} — {subject}", "objectives": "Core learning objectives"}
        for i in range(12)
    ]


# ════════════════════════════════════════════════════════════════════════════
# PERFORMANCE INSIGHTS
# ════════════════════════════════════════════════════════════════════════════

def generate_performance_insight(
    subject:      str,
    avg_score:    float,
    weak_topics:  list,
    num_students: int,
    language:     str = "English",
    provider:     str = None,
) -> Optional[str]:
    """
    Generate an AI-written class performance insight for the teacher dashboard.
    """
    weak_str = ", ".join(weak_topics[:5]) if weak_topics else "none identified yet"
    system   = f"You are an educational data analyst. Write in {language}. Be concise and actionable."
    prompt   = (
        f"Write a short (3-4 sentences) class performance insight for a teacher:\n"
        f"Subject: {subject}\n"
        f"Students assessed: {num_students}\n"
        f"Class average: {avg_score:.1f}%\n"
        f"Weak areas: {weak_str}\n\n"
        f"Include: what the average means, which areas need more practice, "
        f"and one specific teaching recommendation."
    )
    return _ai_call([{"role": "user", "content": prompt}], system, max_tokens=300, provider=provider)


# ════════════════════════════════════════════════════════════════════════════
# TEXTBOOK PHOTO SCANNER (vision)
# ════════════════════════════════════════════════════════════════════════════
# Vision now goes through OpenRouter too, using OpenAI-style image_url/data-URI
# messages (the format OpenRouter expects regardless of which vendor model —
# anthropic/claude-* or openai/gpt-4o — ends up serving the request). If
# OPENROUTER_API_KEY is missing, this returns None and the route layer should
# report a clear "AI vision unavailable" error rather than silently falling
# back to a text-only model that can't see the photo.

_IMG_MEDIA_TYPES = {
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "webp": "image/webp",
    "gif":  "image/gif",
}


def scan_textbook_page(
    image_b64:   str,
    image_ext:   str,
    class_level: str,
    sub_class:   str,
    language:    str,
    provider:    str = None,
) -> Optional[dict]:
    """
    Analyze a photo of a textbook page. Returns:
      {
        "extracted_text": str,   # OCR'd text from the page
        "explanation":    str,   # simplified explanation for the learner's level
        "flashcards":     [{"front": str, "back": str}, ...]  (5 cards)
      }
    Returns None if vision is unavailable or the call fails.
    """
    if not _OPENROUTER_OK:
        return None

    media_type = _IMG_MEDIA_TYPES.get(image_ext.lower(), "image/jpeg")
    age_guide  = _AGE_GUIDES.get(class_level, _AGE_GUIDES["primary"])
    class_label = sub_class or class_level

    system = (
        f"You are an expert tutor reading a photo of a textbook page for a "
        f"{class_label} student. ALWAYS respond in {language}. "
        f"Age guide: {age_guide} "
        f"Return ONLY a valid JSON object, no markdown fences, no preamble."
    )

    prompt_text = (
        f"Read this textbook page photo and return ONLY this JSON:\n"
        f'{{\n'
        f'  "extracted_text": "the text visible on the page, transcribed as accurately as possible",\n'
        f'  "explanation": "a clear, simple explanation of this content for a {class_label} student, in {language}",\n'
        f'  "flashcards": [\n'
        f'    {{"front": "question or term", "back": "answer or definition"}}\n'
        f'    (exactly 5 flashcards covering the key points on this page)\n'
        f'  ]\n'
        f'}}\n'
        f"If the page is unreadable or not educational content, set extracted_text to "
        f'"unreadable" and explain in the explanation field, with an empty flashcards array.'
    )

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt_text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
            },
        ],
    }]

    raw = _ai_call(messages, system, max_tokens=2000, provider=provider)
    if not raw:
        return None

    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        if isinstance(data, dict) and "extracted_text" in data:
            data.setdefault("flashcards", [])
            return data
    except Exception as e:
        print(f"[EDUAI-AI] Textbook scan JSON parse failed: {e} — raw[:200]: {raw[:200]}")
    return None


def generate_quiz_from_text(
    source_text: str,
    class_level: str,
    sub_class:   str,
    language:    str,
    num_q:       int = 5,
    provider:    str = None,
) -> Optional[list]:
    """
    Generate quiz questions from a block of extracted text (e.g. from a
    textbook scan). Same return shape as generate_quiz().
    """
    class_label = sub_class or class_level
    system = (
        f"You are a quiz master. ALL content in {language}. "
        "Return ONLY a valid JSON array, no markdown, no explanation."
    )
    prompt = f"""Based on the following text, generate exactly {num_q} multiple-choice
quiz questions appropriate for a {class_label} student.

Text:
\"\"\"{source_text[:3000]}\"\"\"

Return ONLY this JSON:
[
  {{
    "question": "Question text in {language}",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "correct": 0,
    "explanation": "Why this answer is correct"
  }}
]
"correct" is the 0-based index of the correct option."""

    raw = _ai_call([{"role": "user", "content": prompt}], system, max_tokens=min(3000, num_q * 150), provider=provider)
    if not raw:
        return None
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        if isinstance(data, list):
            return data[:num_q]
    except Exception as e:
        print(f"[EDUAI-AI] Quiz-from-text JSON parse failed: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# PHASE 3 — CHUNK A: Curriculum Gap Detector + Exam Readiness Score
# ════════════════════════════════════════════════════════════════════════════

def detect_curriculum_gaps(
    learner_name: str,
    class_level:  str,
    sub_class:    str,
    quiz_history: list,
    test_history: list,
    language:     str = "English",
    provider:     str = None,
) -> Optional[dict]:

    quiz_history: list of {subject, topic, score_pct, difficulty}
    test_history: list of {subject, class_level, score_pct}  (CBT results)

    Returns: {
        "gaps": [{"subject", "topic", "severity": "high"|"medium"|"low", "recommendation"}],
        "summary": "1-2 sentence overview in {language}"
    }
    or None on failure.
    """
    if not quiz_history and not test_history:
        return {
            "gaps": [],
            "summary": "No quiz or test history yet — complete a few quizzes to get a personalized gap analysis.",
        }

    system = (
        f"You are an expert curriculum analyst for Nigerian schools (NERDC curriculum). "
        f"ALL output text in {language}. "
        "Return ONLY a valid JSON object, no markdown, no explanation."
    )

    quiz_lines = "\n".join(
        f"- {q.get('subject','?')} / {q.get('topic','?')}: {q.get('score_pct',0)}% ({q.get('difficulty','Medium')})"
        for q in quiz_history[:30]
    )
    test_lines = "\n".join(
        f"- {t.get('subject','?')} ({t.get('class_level','?')}): {t.get('score_pct',0)}%"
        for t in test_history[:30]
    )

    prompt = f"""Student: {learner_name}, Class: {sub_class or class_level}

Quiz history (subject / topic: score%):
{quiz_lines or '(none)'}

CBT test history (subject (class): score%):
{test_lines or '(none)'}

Analyze this data and identify the student's weakest topics and subjects.
Group recurring low scores (<60%) as "high" severity, scores 60-74% as "medium",
and any single below-average result as "low". If a subject/topic appears only
once with a high score, do not list it as a gap.

Return ONLY this JSON:
{{
  "gaps": [
    {{
      "subject": "Mathematics",
      "topic": "Fractions",
      "severity": "high",
      "recommendation": "Specific, actionable study suggestion in {language}"
    }}
  ],
  "summary": "1-2 sentence overview of the student's overall standing and top priority, in {language}"
}}

List at most 5 gaps, ordered by severity (high first). If the student has no
clear weak areas, return an empty "gaps" array and a positive "summary"."""

    raw = _ai_call([{"role": "user", "content": prompt}], system, max_tokens=1200, provider=provider)
    if not raw:
        return None
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        if isinstance(data, dict) and "gaps" in data:
            data.setdefault("summary", "")
            return data
    except Exception as e:
        print(f"[EDUAI-AI] Curriculum gap JSON parse failed: {e} — raw[:200]: {raw[:200]}")
    return None


def assess_exam_readiness(
    learner_name: str,
    exam_type:    str,
    subject:      str,
    class_level:  str,
    sub_class:    str,
    quiz_history: list,
    test_history: list,
    language:     str = "English",
    provider:     str = None,
) -> Optional[dict]:
    """
    Estimate a learner's readiness for a target exam (WAEC, JAMB, NECO, etc.)
    in a specific subject, based on their quiz/test history in that subject.

    Returns: {
        "readiness_pct": 0-100,
        "breakdown": {"topic_name": pct, ...},
        "recommendation": "study plan text in {language}"
    }
    or None on failure.
    """
    relevant_quiz = [q for q in quiz_history if (q.get("subject") or "").lower() == subject.lower()]
    relevant_test = [t for t in test_history if (t.get("subject") or "").lower() == subject.lower()]

    if not relevant_quiz and not relevant_test:
        return {
            "readiness_pct": 0.0,
            "breakdown": {},
            "recommendation": (
                f"No {subject} quiz or test history found yet. Take a few practice "
                f"quizzes in {subject} to get a personalized {exam_type} readiness score."
            ),
        }

    system = (
        f"You are an expert {exam_type} exam preparation coach for Nigerian students. "
        f"ALL output text in {language}. "
        "Return ONLY a valid JSON object, no markdown, no explanation."
    )

    quiz_lines = "\n".join(
        f"- Topic: {q.get('topic','General')} — Score: {q.get('score_pct',0)}% ({q.get('difficulty','Medium')})"
        for q in relevant_quiz[:30]
    )
    test_lines = "\n".join(
        f"- CBT test — Score: {t.get('score_pct',0)}%"
        for t in relevant_test[:30]
    )

    prompt = f"""Student: {learner_name}, Class: {sub_class or class_level}
Target exam: {exam_type}, Subject: {subject}

Performance history in {subject}:
{quiz_lines or '(none)'}
{test_lines or '(none)'}

Based on this performance data and typical {exam_type} {subject} difficulty/coverage,
estimate the student's overall readiness percentage (0-100) for the {exam_type}
{subject} paper. Break this down by the topics shown above (estimate a readiness %
per topic). Provide a short, specific recommendation for what to study next.

Return ONLY this JSON:
{{
  "readiness_pct": 0-100 (number, overall estimate),
  "breakdown": {{
    "Topic Name": 0-100
  }},
  "recommendation": "2-3 sentence specific study plan in {language}, naming the weakest topic(s) and a concrete next step"
}}"""

    raw = _ai_call([{"role": "user", "content": prompt}], system, max_tokens=1000)
    if not raw:
        return None
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        if isinstance(data, dict) and "readiness_pct" in data:
            data.setdefault("breakdown", {})
            data.setdefault("recommendation", "")
            try:
                data["readiness_pct"] = float(data["readiness_pct"])
            except (TypeError, ValueError):
                data["readiness_pct"] = 0.0
            return data
    except Exception as e:
        print(f"[EDUAI-AI] Exam readiness JSON parse failed: {e} — raw[:200]: {raw[:200]}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# PHASE 3 — CHUNK B: Report Card Generator + Essay/Assignment Checker
# ════════════════════════════════════════════════════════════════════════════

def generate_report_card(
    learner_name: str,
    class_level:  str,
    sub_class:    str,
    period_start: str,
    period_end:   str,
    quiz_history: list,
    game_history: list,
    streak_days:  int,
    language:     str = "English",
) -> Optional[dict]:
    """
    Generate a narrative report card for a learner covering a date range.

    quiz_history: list of {subject, topic, score_pct, difficulty, date}
    game_history: list of {title, score, date}

    Returns: {
        "overall_grade": "A+"|"A"|"B"|"C"|"D"|"F",
        "content_md": "full markdown report card text in {language}",
        "stats": {"avg_score": float, "quizzes_done": int, "subjects": {"Mathematics": avg_pct, ...}}
    }
    or None on failure.
    """
    system = (
        f"You are a warm, encouraging Nigerian schoolteacher writing a student report card. "
        f"ALL output text in {language}. "
        "Return ONLY a valid JSON object, no markdown fences, no explanation outside the JSON."
    )

    quiz_lines = "\n".join(
        f"- {q.get('date','')}: {q.get('subject','?')} / {q.get('topic','?')} — {q.get('score_pct',0)}% ({q.get('difficulty','Medium')})"
        for q in quiz_history[:40]
    )
    game_lines = "\n".join(
        f"- {g.get('date','')}: {g.get('title','Game')} — score {g.get('score',0)}"
        for g in game_history[:10]
    )

    avg_score = round(sum(q.get("score_pct", 0) for q in quiz_history) / len(quiz_history), 1) if quiz_history else 0

    prompt = f"""Student: {learner_name}, Class: {sub_class or class_level}
Report period: {period_start} to {period_end}
Current activity streak: {streak_days} day(s)

Quiz/test activity in this period:
{quiz_lines or '(no quiz activity recorded this period)'}

Game activity in this period:
{game_lines or '(no game activity recorded this period)'}

Average score this period: {avg_score}%

Write a complete report card for this student. Include:
1. An overall letter grade (A+/A/B/C/D/F) based on the average score
2. A warm opening paragraph
3. A "Strengths" section naming specific subjects/topics where they did well
4. An "Areas to Improve" section naming specific subjects/topics with lower scores, with constructive (not harsh) framing
5. A "Recommendations" section with 2-3 concrete next steps
6. An encouraging closing paragraph

Write the full report as markdown (use ## headers for sections).

Return ONLY this JSON:
{{
  "overall_grade": "A+|A|B|C|D|F",
  "content_md": "## Report Card for {learner_name}\\n\\n... full markdown report in {language} ...",
  "stats": {{
    "avg_score": {avg_score},
    "quizzes_done": {len(quiz_history)},
    "subjects": {{ "SubjectName": average_pct_for_that_subject }}
  }}
}}"""

    raw = _ai_call([{"role": "user", "content": prompt}], system, max_tokens=2000)
    if not raw:
        return None
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        if isinstance(data, dict) and "content_md" in data:
            data.setdefault("overall_grade", "C")
            data.setdefault("stats", {"avg_score": avg_score, "quizzes_done": len(quiz_history), "subjects": {}})
            return data
    except Exception as e:
        print(f"[EDUAI-AI] Report card JSON parse failed: {e} — raw[:200]: {raw[:200]}")
    return None


def check_essay(
    essay_text:  str,
    subject:     str,
    class_level: str,
    sub_class:   str,
    language:    str = "English",
) -> Optional[dict]:
    """
    Provide AI feedback on a student's essay or written assignment.

    Returns: {
        "overall_score": 0-100,
        "strengths": ["..."],
        "weaknesses": ["..."],
        "suggestions": ["..."],
        "summary": "1-2 sentence overall verdict in {language}"
    }
    or None on failure.
    """
    if not essay_text or not essay_text.strip():
        return None

    class_label = sub_class or class_level
    system = (
        f"You are an experienced {subject} teacher grading student writing for a {class_label} "
        f"Nigerian student. ALL feedback text in {language}. "
        "Be constructive and specific — never harsh, never vague. "
        "Return ONLY a valid JSON object, no markdown fences, no explanation outside the JSON."
    )

    prompt = f"""Review the following student essay/assignment for {subject} ({class_label}).

Essay text:
\"\"\"{essay_text[:6000]}\"\"\"

Evaluate it for: content/understanding, structure/organization, grammar/language use,
and (if applicable to {subject}) subject-specific accuracy.

Return ONLY this JSON:
{{
  "overall_score": 0-100 (number),
  "strengths": ["Specific strength 1 in {language}", "Specific strength 2"],
  "weaknesses": ["Specific weakness 1 in {language}", "Specific weakness 2"],
  "suggestions": ["Concrete, actionable suggestion 1 in {language}", "Suggestion 2", "Suggestion 3"],
  "summary": "1-2 sentence overall verdict in {language}, encouraging but honest"
}}

List 2-4 items per array. If the essay is too short or off-topic to evaluate
properly, say so honestly in "summary" and give a low overall_score with
suggestions focused on addressing the prompt."""

    raw = _ai_call([{"role": "user", "content": prompt}], system, max_tokens=1200)
    if not raw:
        return None
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        if isinstance(data, dict) and "overall_score" in data:
            data.setdefault("strengths", [])
            data.setdefault("weaknesses", [])
            data.setdefault("suggestions", [])
            data.setdefault("summary", "")
            try:
                data["overall_score"] = float(data["overall_score"])
            except (TypeError, ValueError):
                data["overall_score"] = 0.0
            return data
    except Exception as e:
        print(f"[EDUAI-AI] Essay check JSON parse failed: {e} — raw[:200]: {raw[:200]}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# PHASE 3 — CHUNK C: Lesson Plan from YouTube Transcript
# ════════════════════════════════════════════════════════════════════════════

def generate_lesson_from_transcript(
    transcript_text: str,
    subject:     str,
    class_level: str,
    sub_class:   str,
    curriculum:  str = "Nigerian (NERDC)",
    language:    str = "English",
) -> Optional[str]:
    """
    Turn a YouTube video transcript into a structured lesson plan.

    Returns markdown lesson plan content, or None on failure.
    Note: this is TRANSCRIPT-based — the AI never sees the video itself,
    only the spoken-word text. Visual-only content (diagrams, on-screen
    text, demonstrations) will not be reflected in the output.
    """
    if not transcript_text or not transcript_text.strip():
        return None

    class_label = sub_class or class_level
    system = (
        f"You are an expert {curriculum} curriculum designer creating a lesson plan "
        f"for {class_label} students in {subject}. ALL output in {language}. "
        "Base the lesson ONLY on the provided transcript content — do not invent "
        "topics not covered in it."
    )

    # Transcripts can be long; cap to keep within reasonable token budget.
    truncated = transcript_text[:12000]
    was_truncated = len(transcript_text) > 12000

    prompt = f"""Below is a transcript of an educational video (spoken-word text only,
no visuals).

Transcript:
\"\"\"{truncated}\"\"\"
{"[transcript truncated — base the lesson on this portion]" if was_truncated else ""}

Create a complete lesson plan for {class_label} based on this video's content.
Include:

## Topic
A clear topic title derived from the transcript content.

## Learning Objectives
3-5 specific, measurable objectives.

## Key Concepts Covered
A summary of the main points from the video, organized clearly.

## Suggested Lesson Flow
- Introduction/hook (how to introduce this topic, referencing the video)
- Main teaching points (drawn from the transcript)
- A suggested point to pause the video for discussion or questions, if applicable
- Wrap-up / summary activity

## Discussion Questions
3-5 questions to check understanding, based on the video's content.

## Suggested Follow-Up Activity
One activity (worksheet, group work, practical task) that reinforces what
was covered in the video.

Write the full lesson plan as markdown with ## headers as shown above, all
in {language}."""

    return _ai_call([{"role": "user", "content": prompt}], system, max_tokens=2500)
