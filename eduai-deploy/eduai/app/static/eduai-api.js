/**
 * eduai-api.js
 * ─────────────────────────────────────────────────────────────
 * Drop-in API client for the AIR-EduAI frontend (learn.html).
 * All calls go to /eduai/* on the same OMEGA Flask server (port 5000).
 * No CORS issues. No external servers.
 *
 * Usage in learn.html:
 *   <script src="/static/eduai-api.js"></script>
 *
 * Then everywhere in the existing JS just call:
 *   await EduAPI.auth.login(email, pass)
 *   await EduAPI.teacher.generateLesson({...})
 *   EduAPI.teacher.generateLessonStream({...}, onDelta, onDone)
 *   etc.
 */

const EduAPI = (() => {
  // ── Base URL — same origin as the Flask server ───────────────────────────
  const BASE = '';   // empty = relative URLs, works on any port

  // ── Token stored in localStorage (same key the existing learn.html uses) ─
  const TOKEN_KEY = 'ea-apikey';  // reuse existing key slot, or use a new one

  function _token() {
    return localStorage.getItem('ea-edutoken') || '';
  }

  function _setToken(t) {
    localStorage.setItem('ea-edutoken', t);
  }

  function _headers(extraHeaders = {}) {
    const h = { 'Content-Type': 'application/json' };
    const t = _token();
    if (t) h['Authorization'] = `Bearer ${t}`;
    return { ...h, ...extraHeaders };
  }

  async function _post(path, body = {}) {
    const r = await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: _headers(),
      body: JSON.stringify(body),
    });
    return r.json();
  }

  async function _get(path, params = {}) {
    const qs = new URLSearchParams(params).toString();
    const url = qs ? `${BASE}${path}?${qs}` : `${BASE}${path}`;
    const r = await fetch(url, { headers: _headers() });
    return r.json();
  }

  async function _del(path) {
    const r = await fetch(`${BASE}${path}`, {
      method: 'DELETE',
      headers: _headers(),
    });
    return r.json();
  }

  /**
   * Stream SSE endpoint.
   * onDelta(text, fullSoFar) called on each chunk.
   * onDone(fullText) called when stream ends.
   * onError(errMsg) called on error.
   */
  async function _stream(path, body, onDelta, onDone, onError) {
    try {
      const r = await fetch(`${BASE}${path}`, {
        method: 'POST',
        headers: _headers(),
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json();
        if (onError) onError(err.error || 'Server error');
        return;
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      let full = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const d = JSON.parse(line.slice(6));
              if (d.error && onError) { onError(d.error); return; }
              if (d.delta) { full = d.full || full; if (onDelta) onDelta(d.delta, full); }
              if (d.done) { if (onDone) onDone(d.full || full); return; }
            } catch (_) {}
          }
        }
      }
      if (onDone) onDone(full);
    } catch (e) {
      if (onError) onError(e.message);
    }
  }

  // ════════════════════════════════════════════════════════════
  // AUTH
  // ════════════════════════════════════════════════════════════
  const auth = {
    async register(payload) {
      const r = await _post('/eduai/auth/register', payload);
      if (r.token) _setToken(r.token);
      return r;
    },
    async login(email, password) {
      const r = await _post('/eduai/auth/login', { email, password });
      if (r.token) _setToken(r.token);
      return r;
    },
    async me() {
      return _get('/eduai/auth/me');
    },
    async logout() {
      const r = await _post('/eduai/auth/logout');
      localStorage.removeItem('ea-edutoken');
      return r;
    },
    async updateLanguage(language, language_name) {
      return _post('/eduai/auth/update-language', { language, language_name });
    },
    isLoggedIn() {
      return !!_token();
    },
  };

  // ════════════════════════════════════════════════════════════
  // TEACHER
  // ════════════════════════════════════════════════════════════
  const teacher = {
    /**
     * Stream lesson note generation.
     * payload: { subject, class_level, sub_class, topic, duration, curriculum, language_name }
     */
    generateLessonStream(payload, onDelta, onDone, onError) {
      return _stream('/eduai/teacher/lesson/generate', payload, onDelta, onDone, onError);
    },

    /** Non-streaming version */
    async generateLesson(payload) {
      return _post('/eduai/teacher/lesson/generate-sync', payload);
    },

    async saveLesson(payload) {
      return _post('/eduai/teacher/lesson/save', payload);
    },

    async listLessons() {
      return _get('/eduai/teacher/lesson/list');
    },

    async deleteLesson(id) {
      return _del(`/eduai/teacher/lesson/${id}`);
    },

    /** Extract SOW topics (multipart/form-data for file uploads) */
    async extractSOW(formData) {
      const r = await fetch(`${BASE}/eduai/teacher/sow/extract`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${_token()}` }, // no Content-Type — browser sets boundary
        body: formData,
      });
      return r.json();
    },

    /**
     * Generate all lesson notes for SOW topics.
     * payload: { sow_id, topics, subject, class_level, curriculum, language_name }
     * onProgress({ progress, total, topic, status }) called per topic.
     * onDone({ generated, total }) called when all done.
     */
    async generateAllSOW(payload, onProgress, onDone, onError) {
      try {
        const r = await fetch(`${BASE}/eduai/teacher/sow/generate-all`, {
          method: 'POST',
          headers: _headers(),
          body: JSON.stringify(payload),
        });
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const d = JSON.parse(line.slice(6));
                if (d.done) { if (onDone) onDone(d); }
                else if (d.error) { if (onError) onError(d.error); }
                else if (onProgress) onProgress(d);
              } catch (_) {}
            }
          }
        }
      } catch (e) {
        if (onError) onError(e.message);
      }
    },

    /** Generate test questions */
    async generateTest(payload) {
      return _post('/eduai/teacher/test/generate', payload);
    },

    async listTestSessions() {
      return _get('/eduai/teacher/test/sessions');
    },

    async getTestResults(sessionId) {
      return _get(`/eduai/teacher/test/results/${sessionId}`);
    },

    async getStats() {
      return _get('/eduai/teacher/stats');
    },

    async getPerformance() {
      return _get('/eduai/teacher/performance');
    },
  };

  // ════════════════════════════════════════════════════════════
  // LEARNER
  // ════════════════════════════════════════════════════════════
  const learner = {
    /**
     * Stream topic explanation.
     * payload: { topic, subject, language_name }
     */
    learnStream(payload, onDelta, onDone, onError) {
      return _stream('/eduai/learner/learn', payload, onDelta, onDone, onError);
    },

    async learn(payload) {
      return _post('/eduai/learner/learn-sync', payload);
    },

    async generateQuiz(payload) {
      return _post('/eduai/learner/quiz/generate', payload);
    },

    async submitQuiz(payload) {
      return _post('/eduai/learner/quiz/submit', payload);
    },

    async quizHistory() {
      return _get('/eduai/learner/quiz/history');
    },

    async tutorChat(message, history = [], language_name = 'English') {
      return _post('/eduai/learner/tutor/chat', { message, history, language_name });
    },

    async dashboard() {
      return _get('/eduai/learner/dashboard');
    },
  };

  // ════════════════════════════════════════════════════════════
  // CBT (no auth — students submit directly)
  // ════════════════════════════════════════════════════════════
  const cbt = {
    async submitResult(payload) {
      // payload: { session_id, student_name, student_class, adm_number, gender, answers, time_taken_s }
      const r = await fetch(`${BASE}/eduai/teacher/test/submit-result`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      return r.json();
    },
  };

  // ════════════════════════════════════════════════════════════
  // GAMES
  // ════════════════════════════════════════════════════════════
  const games = {
    async saveScore(game_id, game_title, score) {
      return _post('/eduai/games/save-score', { game_id, game_title, score });
    },
    async history() {
      return _get('/eduai/games/history');
    },
  };

  // ════════════════════════════════════════════════════════════
  // LEADERBOARD
  // ════════════════════════════════════════════════════════════
  const leaderboard = {
    async get(params = {}) {
      // params: { period:'weekly'|'monthly'|'alltime', class_level, subject, limit }
      return _get('/eduai/leaderboard', params);
    },
  };

  // ════════════════════════════════════════════════════════════
  // BILLING
  // ════════════════════════════════════════════════════════════
  const billing = {
    /** Returns { plan_id, price, subscription_status, trial_ends_at } */
    async planInfo() {
      return _get('/eduai/billing/plan-info');
    },
    /** Call after PayPal onApprove returns a subscriptionID */
    async activate(subscription_id) {
      return _post('/eduai/billing/activate', { subscription_id });
    },
    async cancel() {
      return _post('/eduai/billing/cancel');
    },

    // ── Flutterwave ──────────────────────────────────────────
    /** Returns { plan_id, amount_ngn, public_key, subscription_status, trial_ends_at } */
    async flutterwavePlanInfo() {
      return _get('/eduai/billing/flutterwave/plan-info');
    },
    /** Returns { link, tx_ref } — redirect user to `link` */
    async flutterwaveInitialize() {
      return _post('/eduai/billing/flutterwave/initialize');
    },
    /** Call after Flutterwave redirect returns with transaction_id */
    async flutterwaveActivate(transaction_id) {
      return _post('/eduai/billing/flutterwave/activate', { transaction_id });
    },
    async flutterwaveCancel() {
      return _post('/eduai/billing/flutterwave/cancel');
    },
  };

  // ════════════════════════════════════════════════════════════
  // HEALTH
  // ════════════════════════════════════════════════════════════
  const health = {
    async check() {
      return _get('/eduai/health');
    },
  };

  // Public API
  return { auth, teacher, learner, cbt, games, leaderboard, health, billing };
})();

// ── Convenience: expose token getter for legacy code ────────────────────────
window.EduAPI = EduAPI;
console.log('[EduAPI] ✅ AIR-EduAI API client loaded — all routes → /eduai/*');