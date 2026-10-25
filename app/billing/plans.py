from dataclasses import dataclass

from google.cloud.firestore_v1.base_query import FieldFilter

from app.firebase import db

PLANS_COLLECTION = "plans"


@dataclass
class ModelEntry:
    provider: str
    model: str
    quota: dict | None  # {"requests_per_day": int, "tokens_per_month": int} or None = unlimited


def get_plan(tier_id: str, *, client=None, transaction=None) -> dict:
    ref = (db if client is None else client).collection(PLANS_COLLECTION).document(tier_id)
    doc = ref.get() if transaction is None else ref.get(transaction=transaction)
    if not doc.exists:
        raise ValueError(f"no plan configured for tier {tier_id!r}")
    return doc.to_dict()


def find_plan_by_price_id(price_id: str) -> dict | None:
    query = db.collection(PLANS_COLLECTION).where(filter=FieldFilter("stripe_price_id", "==", price_id)).limit(1)
    docs = list(query.stream())
    if not docs:
        return None
    plan = docs[0].to_dict()
    plan["id"] = docs[0].id
    return plan


def set_stripe_ids(tier_id: str, product_id: str, price_id: str) -> None:
    db.collection(PLANS_COLLECTION).document(tier_id).set(
        {"stripe_product_id": product_id, "stripe_price_id": price_id}, merge=True
    )


# Seed data only - consumed once by `app/billing/setup.py`. Once seeded, Firestore
# is authoritative; editing a live tier means editing Firestore, not this dict.
# Model IDs and OpenRouter's free-tier slug should be re-verified against each
# provider's current catalog before running setup.py in a new environment.
DEFAULT_PLANS: dict[str, dict] = {
    "free": {
        "display_name": "Free",
        "price_cents": 0,
        "stripe_product_id": None,
        "stripe_price_id": None,
        "limits": {"requests_per_day": 50, "tokens_per_month": 200_000},
        "models": [
            {"provider": "anthropic", "model": "claude-haiku-5-5",
             "quota": {"requests_per_day": 20, "tokens_per_month": 50_000}},
            {"provider": "openrouter", "model": "meta-llama/llama-3.2-3b-instruct:free", "quota": None},
        ],
    },
    "pro": {
        "display_name": "Pro",
        "price_cents": 2000,
        "stripe_product_id": None,
        "stripe_price_id": None,
        "limits": {"requests_per_day": 1000, "tokens_per_month": 5_000_000},
        "models": [
            {"provider": "anthropic", "model": "claude-sonnet-5-5",
             "quota": {"requests_per_day": 300, "tokens_per_month": 2_000_000}},
            {"provider": "openrouter", "model": "meta-llama/llama-3.2-3b-instruct:free", "quota": None},
        ],
    },
    "bloom": {
        "display_name": "Bloom",
        "price_cents": 1200,
        "stripe_product_id": None,
        "stripe_price_id": None,
        "limits": {"requests_per_day": 2000, "tokens_per_month": 10_000_000},
        "models": [
            {"provider": "anthropic", "model": "claude-sonnet-5-5",
             "quota": {"requests_per_day": 400, "tokens_per_month": 3_000_000}},
            {"provider": "openai", "model": "gpt-6.1-sol",
             "quota": {"requests_per_day": 400, "tokens_per_month": 3_000_000}},
            {"provider": "openrouter", "model": "meta-llama/llama-3.2-3b-instruct:free", "quota": None},
        ],
    },
    "teams": {
        "display_name": "Teams",
        "price_cents": 4000,
        "stripe_product_id": None,
        "stripe_price_id": None,
        "limits": {"requests_per_day": 3000, "tokens_per_month": 15_000_000},
        "models": [
            {"provider": "anthropic", "model": "claude-sonnet-5-5",
             "quota": {"requests_per_day": 1000, "tokens_per_month": 6_000_000}},
            {"provider": "openai", "model": "gpt-6.1-sol",
             "quota": {"requests_per_day": 500, "tokens_per_month": 3_000_000}},
            {"provider": "openrouter", "model": "meta-llama/llama-3.2-3b-instruct:free", "quota": None},
        ],
    },
    "business": {
        "display_name": "Business",
        "price_cents": 7500,
        "stripe_product_id": None,
        "stripe_price_id": None,
        "limits": {"requests_per_day": 8000, "tokens_per_month": 40_000_000},
        "models": [
            {"provider": "anthropic", "model": "claude-opus-5-5",
             "quota": {"requests_per_day": 1500, "tokens_per_month": 10_000_000}},
            {"provider": "openai", "model": "gpt-6-astra",
             "quota": {"requests_per_day": 1500, "tokens_per_month": 10_000_000}},
            {"provider": "openrouter", "model": "meta-llama/llama-3.2-3b-instruct:free", "quota": None},
        ],
    },
}
