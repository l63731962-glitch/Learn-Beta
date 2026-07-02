"""
eduai/app/scripts/setup_paypal_plans.py
Run ONCE, from the project root, to create your PayPal product + two
monthly billing plans ($3 student, $6 teacher).

    python -m eduai.app.scripts.setup_paypal_plans

Requires PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET already set in .env
(PAYPAL_MODE defaults to "sandbox" — set it to "live" in .env when ready
for real payments).

Copy the three printed lines into your .env file. You only need to run
this script once per PayPal environment (sandbox vs live are separate —
you'll need to run it again after switching PAYPAL_MODE to "live").
"""
from eduai.app.service import paypal_service

if __name__ == "__main__":
    print("Creating PayPal product...")
    product_id = paypal_service.create_product(
        "EduAI Access", "Monthly access to EduAI platform"
    )
    print(f"  → Product created: {product_id}")

    print("Creating student plan ($3/month)...")
    student_plan = paypal_service.create_plan(product_id, "EduAI Student Monthly", "3.00")
    print(f"  → Student plan created: {student_plan}")

    print("Creating teacher plan ($6/month)...")
    teacher_plan = paypal_service.create_plan(product_id, "EduAI Teacher Monthly", "6.00")
    print(f"  → Teacher plan created: {teacher_plan}")

    print("\n" + "=" * 60)
    print("COPY THESE INTO YOUR .env FILE:")
    print("=" * 60)
    print(f"PAYPAL_PRODUCT_ID={product_id}")
    print(f"PAYPAL_STUDENT_PLAN_ID={student_plan}")
    print(f"PAYPAL_TEACHER_PLAN_ID={teacher_plan}")
    print("=" * 60)