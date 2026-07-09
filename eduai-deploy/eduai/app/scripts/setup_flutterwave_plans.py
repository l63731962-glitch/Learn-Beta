"""
eduai/app/scripts/setup_flutterwave_plans.py
Run ONCE to create your two Flutterwave payment plans (₦4,500 student,
₦9,000 teacher).

    python -m eduai.app.scripts.setup_flutterwave_plans

Requires FLUTTERWAVE_SECRET_KEY already set in .env.
"""
import os
import requests

SECRET_KEY = os.getenv("FLUTTERWAVE_SECRET_KEY", "")
BASE_URL = "https://api.flutterwave.com/v3"

STUDENT_PRICE_NGN = 4500
TEACHER_PRICE_NGN = 9000


def _headers():
    return {"Authorization": f"Bearer {SECRET_KEY}", "Content-Type": "application/json"}


def create_plan(name: str, amount_ngn: int, interval: str = "monthly") -> str:
    payload = {
        "amount": amount_ngn,
        "name": name,
        "interval": interval,
        "currency": "NGN",
    }
    r = requests.post(f"{BASE_URL}/payment-plans", headers=_headers(), json=payload, timeout=15)
    r.raise_for_status()
    return str(r.json()["data"]["id"])


if __name__ == "__main__":
    print("Creating student plan...")
    student_id = create_plan("EduAI Student Monthly", STUDENT_PRICE_NGN)
    print(f"  → Student plan created: {student_id}")

    print("Creating teacher plan...")
    teacher_id = create_plan("EduAI Teacher Monthly", TEACHER_PRICE_NGN)
    print(f"  → Teacher plan created: {teacher_id}")

    print("\n" + "=" * 60)
    print("COPY THESE INTO YOUR .env FILE:")
    print("=" * 60)
    print(f"FLUTTERWAVE_STUDENT_PLAN_ID={student_id}")
    print(f"FLUTTERWAVE_TEACHER_PLAN_ID={teacher_id}")
    print("=" * 60)