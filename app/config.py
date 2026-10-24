import os

MAX_TOKENS = 16000
MAX_AGENT_STEPS = 25

BILLING_SUCCESS_URL = os.environ.get("BILLING_SUCCESS_URL", "https://example.com/billing/success")
BILLING_CANCEL_URL = os.environ.get("BILLING_CANCEL_URL", "https://example.com/billing/cancel")
BILLING_PORTAL_RETURN_URL = os.environ.get("BILLING_PORTAL_RETURN_URL", "https://example.com/billing")
