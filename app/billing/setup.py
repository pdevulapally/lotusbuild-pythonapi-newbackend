"""One-off, idempotent: seed Firestore plan docs and create their Stripe Products/Prices.

Run as `python -m app.billing.setup`. Safe to re-run - never overwrites an
existing plan doc or an already-created Stripe price.
"""
import stripe

from app.billing import stripe_client  # noqa: F401 - sets stripe.api_key
from app.billing.plans import DEFAULT_PLANS, set_stripe_ids
from app.firebase import db


def seed_plans() -> None:
    for tier_id, data in DEFAULT_PLANS.items():
        doc = db.collection("plans").document(tier_id)
        if not doc.get().exists:
            doc.set(data)
            print(f"seeded plan {tier_id!r}")
        else:
            print(f"plan {tier_id!r} already exists, left untouched")


def create_stripe_prices() -> None:
    for snapshot in db.collection("plans").stream():
        data = snapshot.to_dict()
        if data["price_cents"] <= 0 or data.get("stripe_price_id"):
            continue
        product = stripe.Product.create(name=data["display_name"], metadata={"tier_id": snapshot.id})
        price = stripe.Price.create(
            product=product.id,
            unit_amount=data["price_cents"],
            currency="usd",
            recurring={"interval": "month"},
        )
        set_stripe_ids(snapshot.id, product.id, price.id)
        print(f"created stripe price for {snapshot.id!r}: {price.id}")


if __name__ == "__main__":
    seed_plans()
    create_stripe_prices()
