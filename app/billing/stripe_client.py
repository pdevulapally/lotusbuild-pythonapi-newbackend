import os

import stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
