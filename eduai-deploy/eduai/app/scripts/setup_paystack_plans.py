"""
eduai/app/scripts/setup_paystack_plans.py
Run ONCE to create your two Paystack billing plans ($3 student, $6 teacher
equivalent — priced in NGN since Paystack's NGN integration is the
smoothest path for Nigerian card/bank/USSD payments).

    python -m eduai.app.scripts.setup_paystack_plans

Requires PAYSTACK_SECRET_KEY already set in .env.

Adjust STUDENT_PRICE_NGN / TEACHER_PRICE_NGN below to whatever your
current USD-to-NGN conversion should be — Paystack has no live FX, so
you set a fixed NGN price yourself.
"""
import os
import requests

SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
BASE_URL = "https://api.paystack.co"

STUDENT_PRICE_NGN = 4500   # ≈ $3 — adjust to your target rate
TEACHER_PRICE_NGN = 9000   # ≈ $6 — adjust to your target rate


def _headers():
    return {"Authorization": f"Bearer {SECRET_KEY}", "Content-Type": "application/json"}


def create_plan(name: str, amount_naira: int, interval: str = "monthly") -> str:
    payload = {
        "name": name,
        "amount": amount_naira * 100,  # Paystack expects kobo
        "interval": interval,
    }
    r = requests.post(f"{BASE_URL}/plan", headers=_headers(), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["data"]["plan_code"]


if __name__ == "__main__":
    print("Creating student plan...")
    student_code = create_plan("EduAI Student Monthly", STUDENT_PRICE_NGN)
    print(f"  → Student plan created: {student_code}")

    print("Creating teacher plan...")
    teacher_code = create_plan("EduAI Teacher Monthly", TEACHER_PRICE_NGN)
    print(f"  → Teacher plan created: {teacher_code}")

    print("\n" + "=" * 60)
    print("COPY THESE INTO YOUR .env FILE:")
    print("=" * 60)
    print(f"PAYSTACK_STUDENT_PLAN_CODE={student_code}")
    print(f"PAYSTACK_TEACHER_PLAN_CODE={teacher_code}")
    print("=" * 60)
