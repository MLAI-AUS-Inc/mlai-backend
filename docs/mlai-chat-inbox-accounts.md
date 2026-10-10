# Server inbox account bindings

Implemented behind `COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED=false`. This is not
a deployment record. No Django migration is needed.

The [coordinated rollout](mlai-chat-inbox-rollout.md) covers the source/export
flags, shadow review and account-erasure prerequisites before real activation.

The backend derives an account key as
`HMAC-SHA256(MLAI_CHAT_ACCOUNT_KEY_SECRET, "<community_id>:<user_id>")`.
The community UUID comes from the authenticated membership adapter capability
response; user IDs and emails never cross this boundary. The dedicated secret
must contain at least 32 UTF-8 bytes and must be identical on every backend web
and maintenance process. There is no fallback to Django's signing secret.

When enabled, device verification binds the installation before committing the
verified status, while holding the existing user/device authority locks. It
requires `member_accounts_v1`, captures the durable relay revocation generation,
and sends `PUT /v2/member-accounts/{pubkey}` with the opaque key and generation.
Retries with the same account and generation are idempotent. A late request
after membership revocation fails the relay generation check. Such a conflict
returns HTTP 409 `device_authority_changed` from verification; availability and
configuration failures return the existing coarse HTTP 503 response.

Revocation continues through `DELETE /v1/members/{pubkey}`. The adapter revokes
membership, pending invites, and the inbox binding in the same relay database
transaction. Shared cursors remain available to the user's other active keys.
The backend does not call a second independent binding revocation endpoint.

Existing verified installations can be previewed in bounded pages:

```sh
python manage.py bind_chat_device_accounts --dry-run --limit 100
python manage.py bind_chat_device_accounts --dry-run --after-device-id 123 --limit 100
```

These commands read backend data and are operational commands: do not run them
on a live system without explicit approval. Dry-run is the default and makes no
adapter requests. `--apply` requires the feature flag, rechecks device authority
under locks, and makes the authenticated relay writes. Resume with the reported
`next_after_device_id`; on failure resume before the reported failed device.
No account keys or installation keys are printed.

**Secret rotation orphans every existing account cursor.** It is not a seamless
key rotation. Keep the secret stable and recover it from the approved secret
store. Planned rotation requires coordinated re-binding using a newer relay
generation (the normal revocation and re-enrolment flow); a conflicting account
cannot overwrite a binding at the same generation. Old opaque cursors require
the account-erasure policy from MLAI Chat ADR-004. Do not automatically delete
old cursors or lower the CAS generation during a rollback.

Database-free validation:

```sh
python scripts/test_without_database.py \
  community_chat.tests.test_inbox_accounts community_chat.tests.test_adapter
```

The migration/backed-test approval requirements in `AGENTS.md` still apply.
