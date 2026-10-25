"""Atomic admission accounting, not an exactly-once provider billing system.

Reserve before effects. Unknown outcomes retain their entire allowance. Ledger
periods are fixed at reservation time, so settlement across midnight is safe.
"""
import datetime
import hashlib
import json
import time
import uuid

from google.cloud.firestore_v1 import transactional
from app.firebase import db

BUDGETS_COLLECTION = "agent_v2_budgets"
RESERVATIONS_COLLECTION = "agent_v2_model_reservations"
MAX_INPUT_BYTES = 96_000
MAX_OUTPUT_TOKENS = 4096


class BudgetExceeded(RuntimeError):
    pass


class CounterUnavailable(BudgetExceeded):
    pass


def positive_int(value):
    if type(value) is not int or value <= 0:
        raise BudgetExceeded("invalid or missing budget limit")
    return value


def _key(*parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def request_allowance(adapter, system, messages, tools, max_tokens):
    """Conservative text allowance; NOT a verified universal tokenizer bound.

    Multimedia and server tools need their own verified metering before enablement.
    Count the rendered wire envelope, not chars/4 or just the user's last message.
    """
    def check(value):
        if isinstance(value, dict):
            if value.get("type") in {"image", "image_url", "input_image", "document", "audio"}:
                raise BudgetExceeded("multimedia metering is not enabled")
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(messages)
    if any(t.get("type", "").startswith("web_search") for t in tools):
        raise BudgetExceeded("server-tool metering is not enabled")
    positive_int(max_tokens)
    if max_tokens > MAX_OUTPUT_TOKENS:
        raise BudgetExceeded("output allowance exceeds per-call cap")
    wire = json.dumps({"system": system, "messages": adapter.render(messages), "tools": tools}, ensure_ascii=False)
    size = len(wire.encode("utf-8"))
    if size > MAX_INPUT_BYTES:
        raise BudgetExceeded("input exceeds per-call byte cap")
    return size * 2 + 4096 + max_tokens


class BudgetLedger:
    def __init__(self, client=db, clock=time.time, transaction_runner=None):
        self.db, self.clock, self.transaction_runner = client, clock, transaction_runner

    def _tx(self, fn):
        return self.transaction_runner(fn) if self.transaction_runner else transactional(fn)(self.db.transaction())

    def _ref(self, collection, key):
        return self.db.collection(collection).document(key)

    @staticmethod
    def _read(ref, tx):
        return ref.get(transaction=tx).to_dict() or {}

    def snapshot(self, uid, period):
        return self._ref(BUDGETS_COLLECTION, _key(uid, period)).get().to_dict() or {}

    def reserve(self, uid, entry, limits, tokens, *, run_id=None):
        positive_int(tokens)
        now = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc)
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        rid = uuid.uuid4().hex  # Generated outside retried transaction; NEVER reused to execute a call.
        refs = [self._ref(BUDGETS_COLLECTION, _key(uid, period)) for period in (day, month)]
        rr = self._ref(RESERVATIONS_COLLECTION, rid)
        model_key = _key(entry.provider, entry.model)
        def reserve(tx):
            states = [self._read(ref, tx) for ref in refs]
            from app.billing.task_bounds import prepare_task_reservation
            task_update = prepare_task_reservation(self.db, tx, uid, run_id, entry, tokens, self.clock)
            for state, field, amount, limit_key in zip(states, ("requests", "tokens"), (1, tokens),
                                                      ("requests_per_day", "tokens_per_month")):
                if state.get("blocked"):
                    raise BudgetExceeded("usage requires manual reconciliation")
                if state.get(field, 0) + amount > positive_int(limits.get(limit_key)):
                    raise BudgetExceeded("tier allowance exhausted")
                model = state.get("models", {}).get(model_key, {})
                if entry.quota is not None and model.get(field, 0) + amount > positive_int(entry.quota.get(limit_key)):
                    raise BudgetExceeded("model allowance exhausted")
            for ref, state in zip(refs, states):
                model = state.setdefault("models", {}).setdefault(model_key, {})
                for target in (state, model):
                    target["requests"] = target.get("requests", 0) + 1
                    target["tokens"] = target.get("tokens", 0) + tokens
                state["uid"] = uid
                tx.set(ref, state)
            if task_update is not None:
                task_update[2]()
                tx.set(task_update[0], task_update[1])
            tx.set(rr, {"uid": uid, "model_key": model_key, "provider": entry.provider, "model": entry.model,
                        "task_budget_id": task_update[0].id if task_update is not None else None,
                        "budget_ids": [ref.id for ref in refs], "tokens": tokens, "state": "reserved",
                        "created_at": self.clock(), "run_id": run_id})
            if task_update is not None:
                task_update[2]()
            return rid
        return self._tx(reserve)

    def prepare_counter(self, tx, uid, *, meter_key, reset_period, hard_limit, run_id):
        """Read/prepare one configured build charge for the caller's transaction.

        Uses the existing ledger/receipts, never a nested transaction. The caller
        commits these writes with queue consumption and ordinary run admission.
        """
        moment = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc)
        period = {"DAILY": moment.strftime("%Y-%m-%d"), "MONTHLY": moment.strftime("%Y-%m"), "NEVER": "never"}[reset_period]
        state_ref = self._ref(BUDGETS_COLLECTION, _key(uid, "counter", meter_key, period))
        receipt_ref = self._ref(RESERVATIONS_COLLECTION, _key(uid, "counter", meter_key, run_id))
        state, receipt = self._read(state_ref, tx), self._read(receipt_ref, tx)
        if receipt:
            # Normal command replay returns before new admission. A charge
            # without its paired admitted command is never a reusable grant.
            raise CounterUnavailable("counter receipt requires reconciliation")
        used = state.get("consumed") if state else 0
        if (type(used) is not int or used < 0 or (state and (state.get("uid"), state.get("meter_key"), state.get("period")) != (uid, meter_key, period))):
            raise CounterUnavailable("counter ledger requires reconciliation")
        if state.get("blocked"):
            raise CounterUnavailable("counter ledger requires reconciliation")
        if used >= hard_limit:
            raise BudgetExceeded("configured build allowance exhausted")
        return [(state_ref, {"uid": uid, "meter_key": meter_key, "period": period, "consumed": used + 1}),
                (receipt_ref, {"uid": uid, "run_id": run_id, "meter_key": meter_key, "kind": "counter",
                               "state": "consumed", "quantity": 1, "created_at": self.clock()})]

    def settle(self, rid, usage):
        # Missing, negative, bool, or partial usage cannot justify any refund.
        values = [usage.get(k) for k in ("input_tokens", "output_tokens")]
        if any(type(v) is not int or v < 0 for v in values):
            raise BudgetExceeded("invalid provider usage; reservation retained")
        actual = sum(values)
        rr = self._ref(RESERVATIONS_COLLECTION, rid)
        def settle(tx):
            reservation = self._read(rr, tx)
            if not reservation:
                raise BudgetExceeded("missing reservation")
            if reservation["state"] != "reserved":
                return reservation["state"] == "overrun"
            refs = [self._ref(BUDGETS_COLLECTION, key) for key in reservation["budget_ids"]]
            states = [self._read(ref, tx) for ref in refs]
            task_ref = self._ref(BUDGETS_COLLECTION, reservation["task_budget_id"]) if reservation.get("task_budget_id") else None
            task_state = self._read(task_ref, tx) if task_ref is not None else None
            overrun = actual > reservation["tokens"]
            for ref, state in zip(refs, states):
                if not state or reservation["model_key"] not in state.get("models", {}):
                    raise BudgetExceeded("inconsistent usage ledger; reservation retained")
                delta = actual - reservation["tokens"]
                state["tokens"] += delta
                state["models"][reservation["model_key"]]["tokens"] += delta
                if overrun:
                    state["blocked"] = True
                tx.set(ref, state)
            if task_ref is not None:
                if not task_state or type(task_state.get("actual_tokens")) is not int:
                    raise BudgetExceeded("task accounting requires reconciliation")
                task_state["actual_tokens"] += actual
                if overrun:
                    task_state["blocked"] = True
                tx.set(task_ref, task_state)
            reservation.update(state="overrun" if overrun else "settled", actual_tokens=actual)
            tx.set(rr, reservation)
            return overrun
        if self._tx(settle):
            raise BudgetExceeded("provider exceeded reservation; execution stopped for reconciliation")


budgets = BudgetLedger()


def metered_create(uid, adapter, entry, *, system, messages, tools, max_tokens):
    from app.billing.entitlements import resolve_entitlements
    from app.execution.context import checkpoint, run_id
    checkpoint()
    entitlements = resolve_entitlements(uid)
    from app.billing.model_select import resolve_model
    current = resolve_model(entitlements, entry.model, provider=entry.provider)
    allowance = request_allowance(adapter, system, messages, tools, max_tokens)
    rid = budgets.reserve(uid, current, entitlements.limits, allowance, run_id=run_id())
    checkpoint()  # A lost lease retains reservation rather than risk a duplicate spend.
    response = adapter.create(model=current.model, system=system, messages=messages, tools=tools, max_tokens=max_tokens)
    # Settle even if cancelled meanwhile: observed usage is safe accounting, not
    # authority to continue execution. Any exception above retains reservation.
    budgets.settle(rid, adapter.usage(response))
    checkpoint()
    return response
