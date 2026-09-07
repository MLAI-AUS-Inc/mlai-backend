# Community Chat account profiles

This is the source contract for the account-profile implementation. It does
not establish deployment status. Repository overview labels referring to an
inactive Chat experiment conflict with the active bridge description in the
architecture table; verify runtime deployment independently of those labels.

`GET /api/v1/community-chat/account/` returns the authenticated member's own
profile, public projection, session and devices. `PATCH` now uses the same
Chat-scoped authentication and response shape. Cookie writes require the exact
origin bound to the session; native clients use the protected bearer token.
The session includes `public_key` so native clients can verify that the active
chat signer belongs to the authenticated account before editing its profile.

Send `profile_version` exactly as last returned by GET and one or more of:

| Field | Accepted value |
| --- | --- |
| `display_name` | Trimmed, non-empty string, up to 80 characters |
| `about` | Trimmed string up to 500 characters; empty clears the bio |
| `avatar_url` | HTTP(S) image URL up to 200 characters, or null to remove |

The avatar limit follows the existing `core.User.avatar_url` column. Clients
must upload inline emoji/generated images through their authenticated media
pipeline before saving the resulting URL. The backend never fetches a supplied
avatar URL. Embedded credentials and other URL schemes are rejected.

Unknown fields, including account IDs, email, password and role fields, are
rejected. The caller's account is derived solely from its session. Display name
uses the existing first/last-name fields; this endpoint creates no schema.

A stale version returns 409 with `profile_version_conflict`. Clients retain the
draft and reload the account before asking the member to review and save again.
Updates take the existing user-before-session lock order and recheck active
account, session expiry, revocation, auth version and token rotation under the
lock. The profile timestamp advances monotonically, including clock rollback.
Only the selected public fields and timestamp are written.

The account update is authoritative. Clients durably queue the returned public
projection for their active device key and then publish its kind-0 relay
metadata. A relay outage does not roll back the account. Enrollment on another
device reads the new canonical profile. Email/internal account fields must
never enter the public relay projection.

All 10 tests in `community_chat.tests.test_account_profiles` passed on isolated
PostgreSQL 18.6 with pgvector 0.8.2, using the exact approved 301-migration closure
in [slack-chat-test-proposal.md](slack-chat-test-proposal.md). This includes the
concurrent-write regression and session revocation/rotation checks. The runner
removed its test database after completion. This proves the backend source
behavior; it does not establish deployment or a physical-device save.

The broader Slack/Volunteer regressions exposed missing test prerequisites in
that original proposal. The complete setup is separately listed in
[community-chat-full-test-proposal.md](community-chat-full-test-proposal.md).
