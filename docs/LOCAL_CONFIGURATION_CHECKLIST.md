# Local configuration names checklist

The user confirmed local environment variables have not been set. No secret
files were searched or read, and no credentials were created. Missing local
configuration is separate from an implementation test failure. Names below come
from the existing example/configuration; this is not setup or live-call approval.

Offline tests inject synthetic values for these names only:

- ANTHROPIC_API_KEY
- OPENAI_API_KEY
- OPENROUTER_API_KEY
- E2B_API_KEY
- STRIPE_SECRET_KEY
- STRIPE_WEBHOOK_SECRET

Emulator runs additionally use FIRESTORE_EMULATOR_HOST with anonymous credentials
and an explicit local demo project. They do not require or initialize Firebase
service-account configuration.

Existing real integration configuration names, still unset/unverified here:

- FIREBASE_SERVICE_ACCOUNT_JSON
- ANTHROPIC_API_KEY
- OPENAI_API_KEY
- OPENROUTER_API_KEY
- OPENROUTER_BASE_URL
- E2B_API_KEY
- E2B_CODE_TEMPLATE
- E2B_CODE_TIMEOUT
- E2B_DESKTOP_TEMPLATE
- E2B_DESKTOP_TIMEOUT
- REDIS_URL
- STRIPE_SECRET_KEY
- STRIPE_WEBHOOK_SECRET
- BILLING_SUCCESS_URL
- BILLING_CANCEL_URL
- BILLING_PORTAL_RETURN_URL
- GITHUB_CLIENT_ID
- GITHUB_CLIENT_SECRET
- GITHUB_REDIRECT_URI
- GITHUB_OAUTH_SUCCESS_REDIRECT_URL
- CREDENTIAL_ENCRYPTION_KEY
- ALLOWED_HOSTS
- ALLOWED_ORIGINS
- ENABLE_EXPERIMENTAL_WORKER

Keep the experimental worker guard disabled outside separately authorized tests.
Environment variables alone cannot supply the missing verified effort catalog
or paid effort ceiling: those require the existing product configuration/schema,
an explicit data/migration review, and fixture-tested provider metadata retrieval
before any separately authorized live validation. Do not infer them from tier
names, provider names or example seed models.


GitHub broker continuation: `GITHUB_WRITE_BROKER.md` adds no configuration names,
enabling ENV switch, real token/installation setup or production wiring. The
existing OAuth names above cannot provide trusted repository/installation policy
or enable writes. Fresh auth, scoped issuance/revocation and named-branch approval
policy remain verified-service prerequisites, with exact-diff approval unsupported.

Scoped task source continuation: no enabling ENV variable is added.
Required new native config/task_authorization names are schema_version, version,
max_duration_seconds, max_turns, max_run_seconds, max_model_requests,
max_reserved_tokens, reader_timeout_seconds, evidence_ttl_seconds, allowed_modes,
allowed_models, allowed_tools. No values are seeded. Trusted source composition
requires explicit BoundedAccountReader.max_concurrent and TaskQueueAuthorizer;
default repository reader and worker authorizer remain None. See
TASK_AUTHORIZATION_CONTRACT.md. No actual grants or SDK account lookups occurred.
