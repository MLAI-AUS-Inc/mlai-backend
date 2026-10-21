# Alternate sign-in emails and account repairs

Operator-managed `core.UserEmailAlias` records reserve additional email identifiers
for an existing user. Primary and alternate identifiers normalize to lowercase.
Password/JWT login, account lookup, magic-link issuance, password reset, Chat email
codes and signup reuse the canonical user. Outbound authentication messages go to
the primary address. Synthetic Slack placeholder aliases never represent verified
mailboxes. Existing bearer links for deleted source accounts do not transfer access.

PostgreSQL transaction advisory locks serialize primary/alias claims across both
tables, alongside case-insensitive unique indexes. Create identities through model
save or `get_or_create_for_email`; bulk creation, QuerySet email updates and raw SQL
bypass the shared namespace guard and must not be used for identity assignments.
Aliases are read-only in admin. Verified Slack profile aliases resolve the primary
account without replacing its selected Slack identity.

`merge_accounts_with_aliases` is a specifically reviewed operational repair, defaulting
to a transaction that rolls back. It rejects unknown related records, privileged or
banned accounts, explicit founder links, conflicting profiles, external identities
on sources, and purchased/digital balances. A commit requires a superuser attribution,
a private server-side recovery directory, a unique repair key and explicit final
balance. It preserves ledger and booking records, revokes source device authority
through the existing relay boundary, invalidates source credentials, retains the
primary user ID and integrations, and records a balance adjustment and admin audit.
A committed repair key is a no-op on retry; it never resets subsequent spending.

Before running, inspect the exact migration plan and obtain approval for
`core.0070_user_email_alias`. Apply only that migration in production. Disposable
regressions use the separately approved, hash-pinned 367-migration inventory in
`email-alias-test-migrations-2026-10-21.json`; production credentials/data are excluded.

The 21 October rollout is based on the running `f5491f5581e2` release because newer
main releases had rolled back. Its image layers must use the actual running image
IDs, not the mutable latest tags, and include no unrelated main migrations.
