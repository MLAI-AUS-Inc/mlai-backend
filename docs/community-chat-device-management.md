# MLAI Chat installation lifecycle

Each registration identifies a signing key and an app installation/browser
profile, not a physical-device fingerprint. Clients reuse the current signer
and installation for a known matching account after a fresh authentication
proof. Account changes use a new isolated signer. Locally cached ownership
hints cannot authorize backend access or bypass key ownership checks.

## Private device management

All routes require a Community Chat account session and community admission.
Bootstrap tokens and generic account credentials cannot use these routes.
Cookie mutations preserve exact-origin validation. Responses never include
credentials, signing keys or another owner's registrations.

- `GET /api/v1/community-chat/account/devices/` returns owned active
  registrations with row ID, public key, installation ID, client, platform,
  friendly name, status, creation date, current-device marker,
  `last_authenticated_at`, and agent connection names. Cache policy is
  `no-store`. Activity is derived from actual authenticated account-session
  use, including historical sessions, rather than the device verification
  timestamp. It does not claim online presence or activity from an offline
  relay-only device.
- `PATCH /api/v1/community-chat/account/devices/<uuid>/` accepts only
  `{"name":"My laptop browser"}`. The nonempty name is at most 120
  characters. Owner and current credential are rechecked under the existing
  user-first lock. Renaming does not change membership or signing authority.
- `DELETE /api/v1/community-chat/account/devices/<uuid>/` requires
  `{"confirmed":true,"expected_public_key":"<64 lowercase hex>"}` and
  an account authentication within the last ten minutes. Refreshing a session
  does not satisfy fresh authentication. A stale row/key, another owner's row,
  the current installation, or an active agent anchor is rejected. Use the
  existing self-removal flow for the current app; reconnect or disconnect an
  anchored agent before removing that registration.

Removal uses `revoke_device_authority`, preserving its existing lock and relay
generation fences. It revokes credentials and associated relay/private
registration access while retaining device history and message attribution.
The old key-scoped `/devices/<public_key>/` self-removal endpoint is unchanged.
No new schema, automatic retirement job, physical-device fingerprinting, or
cross-account key rebinding is introduced.

The browser/desktop account provider checks account state every five minutes
while active, making authenticated use visible without updating activity on
every message. The device list says last used rather than online. Owners can
rename entries and explicitly review removal in Settings > Profile > Your
devices. Native mobile reauthentication also reuses its protected same-account
registration; mobile code requires the normal mobile release before users
receive it.

## Validation

`community_chat.tests.test_device_management` covers owner-only lists, actual
session activity, exact-origin checks, bootstrap rejection, rename isolation,
recent authentication, explicit confirmation, stale targets, current-device
and agent protection, and credential/relay revocation. Existing account,
password, device-binding and agent PostgreSQL regressions must remain green.
Disposable tests require approval of their exact existing migration inventory.
