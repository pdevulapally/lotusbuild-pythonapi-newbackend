import stripe
from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import get_uid
from app.billing import stripe_client  # noqa: F401 - sets stripe.api_key
from app.billing.plans import find_plan_by_price_id, get_plan
from app.config import BILLING_CANCEL_URL, BILLING_PORTAL_RETURN_URL, BILLING_SUCCESS_URL
from app.firebase import db
from app.idempotency import require_idempotency_key, run_idempotent
from app.models import CheckoutIn
from app.ratelimit import rate_limit_dependency
from app.security_log import log_security_event

router = APIRouter(prefix="/billing")


def _get_or_create_customer(uid: str, idempotency_key: str) -> str:
    sub_ref = db.collection("subscriptions").document(uid)
    sub = sub_ref.get()
    customer_id = (sub.to_dict() or {}).get("stripe_customer_id") if sub.exists else None
    if customer_id:
        return customer_id
    customer_id = stripe.Customer.create(
        metadata={"uid": uid}, idempotency_key=f"{idempotency_key}:customer"
    ).id
    sub_ref.set({"stripe_customer_id": customer_id}, merge=True)
    return customer_id


@router.post("/checkout")
def checkout(
    body: CheckoutIn,
    uid: str = Depends(get_uid),
    idempotency_key: str = Depends(require_idempotency_key),
    _rl: None = Depends(rate_limit_dependency("mutate")),
):
    def do():
        plan = get_plan(body.tier_id)
        if not plan.get("stripe_price_id"):
            raise HTTPException(status_code=400, detail=f"tier {body.tier_id!r} has no price configured")

        customer_id = _get_or_create_customer(uid, idempotency_key)
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": plan["stripe_price_id"], "quantity": 1}],
            subscription_data={"metadata": {"uid": uid, "tier_id": body.tier_id}},
            success_url=BILLING_SUCCESS_URL,
            cancel_url=BILLING_CANCEL_URL,
            idempotency_key=f"{idempotency_key}:session",
        )
        return 200, {"url": session.url}

    return run_idempotent(uid, "billing:checkout", idempotency_key, do).body


@router.post("/portal")
def portal(uid: str = Depends(get_uid)):
    sub = db.collection("subscriptions").document(uid).get()
    customer_id = (sub.to_dict() or {}).get("stripe_customer_id") if sub.exists else None
    if not customer_id:
        raise HTTPException(status_code=400, detail="no billing customer for this user yet")
    session = stripe.billing_portal.Session.create(customer=customer_id, return_url=BILLING_PORTAL_RETURN_URL)
    return {"url": session.url}


def _upsert_subscription(obj: dict) -> None:
    uid = obj["metadata"]["uid"]
    price_id = obj["items"]["data"][0]["price"]["id"]
    plan = find_plan_by_price_id(price_id)
    if plan is None:
        raise RuntimeError(f"stripe price {price_id!r} matches no plan - plan data and Stripe are out of sync")
    db.collection("subscriptions").document(uid).set(
        {
            "tier_id": plan["id"],
            "stripe_subscription_id": obj["id"],
            "status": obj["status"],
            "current_period_end": obj["current_period_end"],
        },
        merge=True,
    )


def _cancel_subscription(obj: dict) -> None:
    uid = obj["metadata"]["uid"]
    db.collection("subscriptions").document(uid).set({"tier_id": "free", "status": "canceled"}, merge=True)


WEBHOOK_HANDLERS = {
    "customer.subscription.created": _upsert_subscription,
    "customer.subscription.updated": _upsert_subscription,
    "customer.subscription.deleted": _cancel_subscription,
}


@router.post("/webhook")
async def webhook(request: Request):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, request.headers.get("stripe-signature", ""), stripe_client.WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        log_security_event("webhook_signature_failure", error=str(exc))
        raise HTTPException(status_code=400, detail=f"invalid webhook signature: {exc}")

    def do():
        handler = WEBHOOK_HANDLERS.get(event["type"])
        if handler:
            handler(event["data"]["object"])
        return 200, {"received": True}

    # Dedupe by Stripe's own event id (no uid - Stripe is the caller, not a user).
    return run_idempotent(None, "webhook", event["id"], do).body
