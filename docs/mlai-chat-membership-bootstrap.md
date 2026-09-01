# MLAI Chat membership bootstrap

MLAI accounts and Buzz/Nostr device keys remain separate security identities.
The `community_chat` Django app binds them only after the logged-in MLAI user
proves control of the device key with a short-lived Nostr event.

## Browser-to-desktop account handoff

The desktop app never receives the browser's HttpOnly MLAI Chat cookies.
Instead, it uses a short-lived OAuth-style handoff bound to the exact desktop
key, installation metadata, approved Tauri origin, random state, and PKCE S256
verifier:

1. The desktop creates a device key, installation ID, random state and PKCE
   verifier. It calls `POST /api/v1/community-chat/auth/device/start/` with
   `client_id=mlai-chat-desktop`, the device metadata, state, and S256 code
   challenge. Exact `tauri://localhost` and `http://tauri.localhost` origins
   receive credential-free CORS on the Community Chat API namespace; native
   calls omit ambient cookies and authenticate with installation-scoped bearer
   credentials after this start/exchange flow.
2. The desktop opens the returned `/auth/desktop?request=...` URL on
   `chat.mlai.au`. An unauthenticated user first completes the normal MLAI Chat
   email-code sign-in in the browser. The callback then shows an explicit
   approval action and calls `POST .../auth/device/authorize/` with that
   browser's origin-bound MLAI Chat cookie session. The API returns a
   purpose-salted, timestamped authorization code bound to that request and
   browser user; the browser returns it only through
   `mlaichat://auth/callback` (with a copyable callback URL as a manual
   fallback).
3. After receiving that callback, the desktop calls
   `POST .../auth/device/exchange/` once with the signed authorization code,
   request ID, original state and verifier, plus the same validated device
   metadata. The code is not delivered to the app that merely knows or polls
   the request UUID.
4. One successful exchange atomically returns a short-lived
   `mlai_chat_...` bootstrap credential and rotating, Chat-scoped native access
   and refresh credentials. The desktop keeps the bootstrap credential only in
   memory for enrollment and stores the account session in OS-protected
   storage.
5. The request is consumed once. The state, verifier, complete enrollment
   context, request origin, signed-code request/user binding, expiry, and public
   key are checked under a row lock before any credential is issued. The
   callback contains the short-lived authorization code, but never the state,
   verifier, bootstrap credential, or account access/refresh token; the code is
   unusable without the desktop's PKCE verifier and becomes unusable when the
   request is consumed.

The native account session cannot authenticate any non-Chat MLAI API and is
bound to the registered desktop installation. Hosted web clients keep using
HttpOnly cookies, while mobile clients retain their in-app email-code flow and
the registered `mlaichat://callback` application origin. That mobile origin is
an API enrollment boundary, not a browser CORS origin. All clients enter the
same membership request flow below.

## Request flow

1. `GET /api/v1/community-chat/session/` returns account eligibility and the
   user's public device bindings.
2. `POST .../bootstrap/challenge/` binds a nonce to the user, public key,
   `community-chat:enrol-device` action, API audience, and exact client origin.
3. The client signs the returned unsigned kind `27235` event with the device
   key and sends it to `POST .../bootstrap/invite/`.
4. The backend verifies the event id and BIP-340 signature and atomically
   consumes the challenge. While holding the device-authority lock, it requires
   the adapter's authenticated `generation_cas_v2` capability, captures the
   current per-key generation with `POST /v2/member-invite-intents`, and passes
   that exact `expected_generation` to `POST /v2/member-invites`. It never falls
   back to the legacy one-request mint. The resulting five-minute, one-use
   `member` invite ID is audited under the same lock; the invite code is not.
5. The client claims the invite directly against `chat.mlai.au`, then calls
   `POST .../bootstrap/confirm/`. Only a relay role of exactly `member` is
   accepted.
6. `DELETE .../devices/{pubkey}/` first cancels every unconfirmed audited invite,
   then asks the adapter to remove exactly a `member` and advance that key's
   durable enrollment generation. It marks the binding revoked while retaining
   its audit history. Repeating the delete is safe and advances the same
   delete-wins fence, including recovery after an adapter mint whose backend
   transaction did not commit.

The backend stores adapter invite IDs and expiry metadata, never invite codes,
signed proof payloads, email addresses, raw chat content, or private keys in
membership audit records.

The adapter serializes invite mint, invite cancellation, membership claim, and
member deletion by community and public key. A member delete durably advances a
per-key generation before it returns. An older invite mint that finishes later
cannot bind or redeem in that newer generation; an explicit later enrollment
starts from the new generation. Generic operator-created relay invites are not
bound to this device fence and are not deleted by these endpoints.

The intent phase creates no invite or membership capability. If it times out
and reaches the adapter only after a delete, its caller has no mint response and
cannot continue. If the mint phase is delayed until after a delete, its captured
generation is stale and the adapter returns `invite_attempt_revoked` without
creating an invite. Capability discovery, intent capture, and mint all require
the private adapter bearer token.

## Private adapter boundary

Run `buzz-membership-adapter` beside the relay on a loopback/private address.
It requires an exact bearer token and refuses public bind addresses. Configure:

```text
DATABASE_URL=postgres://...
RELAY_URL=wss://chat.mlai.au
MLAI_MEMBERSHIP_ADAPTER_BIND=127.0.0.1:3100
MLAI_MEMBERSHIP_ADAPTER_TOKEN=<independent 32+ byte secret>
MLAI_BOOTSTRAP_PRIVATE_KEY=<independent Nostr service key>
```

The service key must not be the relay owner key or Slack bridge key. The
adapter's database calls hardcode `max_uses=1`, the shared invite claim path
hardcodes `role=member`, and revocation uses an atomic `role=member` predicate.
It has no endpoint for role changes, owners/admins, arbitrary signing, relay
configuration, or key export.
