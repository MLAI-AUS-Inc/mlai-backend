# MLAI Chat membership bootstrap

MLAI accounts and Buzz/Nostr device keys remain separate security identities.
The `community_chat` Django app binds them only after the logged-in MLAI user
proves control of the device key with a short-lived Nostr event.

## Request flow

1. `GET /api/v1/community-chat/session/` returns account eligibility and the
   user's public device bindings.
2. `POST .../bootstrap/challenge/` binds a nonce to the user, public key,
   `community-chat:enrol-device` action, API audience, and exact client origin.
3. The client signs the returned unsigned kind `27235` event with the device
   key and sends it to `POST .../bootstrap/invite/`.
4. The backend verifies the event id and BIP-340 signature, atomically consumes
   the challenge, and asks the private adapter for a five-minute, one-use
   `member` invite.
5. The client claims the invite directly against `chat.mlai.au`, then calls
   `POST .../bootstrap/confirm/`. Only a relay role of exactly `member` is
   accepted.
6. `DELETE .../devices/{pubkey}/` asks the adapter to remove exactly a
   `member`, then marks the binding revoked while retaining its audit history.

The backend stores adapter invite IDs and expiry metadata, never invite codes,
signed proof payloads, email addresses, raw chat content, or private keys in
membership audit records.

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

