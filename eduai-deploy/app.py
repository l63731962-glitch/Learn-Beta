"""
app.py — EduAI standalone deployment entry point
"""
import os
import sys
from flask import Flask, send_file
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

ROOT = os.path.dirname(os.path.abspath(__file__))
EDUAI_ROOT = os.path.join(ROOT, "eduai")

if EDUAI_ROOT not in sys.path:
    sys.path.insert(0, EDUAI_ROOT)

app = Flask(__name__, static_folder=os.path.join(ROOT, "eduai", "app", "static"))
CORS(app)

def _find_learn_html():
    candidates = [
        os.path.join(ROOT, "eduai", "frontend", "learn.html"),
        os.path.join(ROOT, "eduai", "static", "learn.html"),
        os.path.join(ROOT, "eduai", "learn.html"),
        os.path.join(ROOT, "learn.html"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

@app.route("/")
def index():
    
    path = _find_learn_html()
    if path:
        return send_file(path)
    return "<h2>learn.html not found</h2><p>Place it in <code>eduai/frontend/learn.html</code></p>", 404


@app.route("/learn")
def learn_page():
    path = _find_learn_html()
    if path:
        return send_file(path)
    return "<h2>learn.html not found</h2><p>Place it in <code>eduai/frontend/learn.html</code></p>", 404

try:
    from eduai.app.routes.eduai_routes import register_eduai_routes
    register_eduai_routes(app)
    print("[EDUAI] ✅ Core routes registered")
except Exception as e:
    print(f"[EDUAI] ⚠️  Core routes: {e}")

try:
    from eduai.app.routes.community_routes import register_community_routes
    register_community_routes(app)
    print("[EDUAI] ✅ Community routes registered")
except Exception as e:
    print(f"[EDUAI] ⚠️  Community routes: {e}")

try:
    from eduai.app.routes.phase1_routes import register_phase1_routes
    register_phase1_routes(app)
    print("[EDUAI] ✅ Phase1 routes registered")
except Exception as e:
    print(f"[EDUAI] ⚠️  Phase1 routes: {e}")

try:
    from eduai.app.routes.phase2_routes import register_phase2_routes
    register_phase2_routes(app)
    print("[EDUAI] ✅ Phase2 routes registered")
except Exception as e:
    print(f"[EDUAI] ⚠️  Phase2 routes: {e}")

try:
    from eduai.app.routes.phase3_routes import register_phase3_routes
    register_phase3_routes(app)
    print("[EDUAI] ✅ Phase3 routes registered")
except Exception as e:
    print(f"[EDUAI] ⚠️  Phase3 routes: {e}")

try:
    from eduai.app.routes.i18n_routes import register_i18n_routes
    register_i18n_routes(app)
    print("[EDUAI] ✅ i18n routes registered")
except Exception as e:
    print(f"[EDUAI] ⚠️  i18n routes: {e}")

try:
    from eduai.app.routes.billing_routes import register_billing_routes
    register_billing_routes(app)
    print("[EDUAI] ✅ Billing routes registered")
except Exception as e:
    print(f"[EDUAI] ⚠️  Billing routes: {e}")

if __name__ == "__main__":
    learn_path = _find_learn_html()
    print("=" * 50)
    print("  EduAI — http://127.0.0.1:5000/learn")
    if learn_path:
        print(f"  learn.html found at: {learn_path}")
    else:
        print("  ⚠️  learn.html NOT FOUND — place it in eduai/frontend/learn.html")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
