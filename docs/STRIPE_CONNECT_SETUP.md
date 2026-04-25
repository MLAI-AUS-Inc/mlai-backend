# Stripe Connect Setup

The Vibe Raising Stripe connector uses Stripe Connect OAuth for founder-authorized
access to an existing Stripe account. The backend must have Stripe Connect
credentials before `/integrations/connect/stripe` can redirect to Stripe.

## Required Backend Environment

Use these variables in production:

```sh
STRIPE_CONNECT_CLIENT_ID=ca_...
STRIPE_SECRET_KEY=sk_live_...
STRIPE_OAUTH_REDIRECT_URI=https://api.mlai.au/integrations/callback/stripe
STRIPE_OAUTH_SCOPES=read_only
```

For local testing against the backend on port 8000:

```sh
STRIPE_CONNECT_CLIENT_ID=ca_...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_OAUTH_REDIRECT_URI=http://localhost:8000/integrations/callback/stripe
STRIPE_OAUTH_SCOPES=read_only
```

Do not use the literal placeholders above. `STRIPE_CONNECT_CLIENT_ID` must be
the full Connect OAuth client ID from Stripe's Connect OAuth application
settings, not a publishable key, account ID, or shortened `ca_...` placeholder.
If Stripe returns `No application matches the supplied client identifier`, the
client ID being sent to Stripe is not a real Connect OAuth client ID for that
Stripe platform.

`STRIPE_CONNECT_OAUTH_CLIENT_ID` or `STRIPE_CLIENT_ID` can be used as aliases for
`STRIPE_CONNECT_CLIENT_ID`. `STRIPE_API_SECRET_KEY` or `STRIPE_API_KEY` can be
used as aliases for `STRIPE_SECRET_KEY`, but the canonical names above are
preferred for deployment.

## Stripe Dashboard Setup

In the Stripe dashboard, enable Connect OAuth for the platform and add the exact
redirect URI for the environment being tested:

- Production: `https://api.mlai.au/integrations/callback/stripe`
- Local: `http://localhost:8000/integrations/callback/stripe`

Use a test-mode Connect client ID with a test secret key, and a live-mode Connect
client ID with a live secret key. Stripe rejects token exchange when the client ID
mode and secret key mode do not match.

Run this before testing locally or deploying:

```sh
APP_ENV=local ./venv/bin/python manage.py check_stripe_connect
```

## Flow

1. Frontend redirects the founder to `/integrations/connect/stripe?next=...`.
2. Backend stores OAuth `state` in the founder session.
3. Backend redirects to `https://connect.stripe.com/oauth/authorize`.
4. Stripe redirects back to `/integrations/callback/stripe` with `code` and
   `state`.
5. Backend validates state, exchanges the code at Stripe, and stores the
   resulting connection in `ExternalServiceConnection`.

Tokens are encrypted at rest with `EncryptedTextField`.
