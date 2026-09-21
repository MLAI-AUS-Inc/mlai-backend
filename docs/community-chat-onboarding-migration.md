# Proposed migration: community_chat.0012_member_onboarding

Approved explicitly on 21 September 2026: creation, disposable local/CI
database execution with existing dependencies, and production application
during the requested rollout. The additive migration file is committed with
the implementation; execution evidence is recorded separately.

The implementation models are in `community_chat/onboarding_models.py` and
`community_chat/models.py`. The proposed additive migration will:

1. Create `CommunityMemberProfile`: one private application per account; proposed
   name, admission status, 18+ attestation time, accepted policy version,
   optional city/interests/marketing preference, and review timestamps/reviewer.
   Add the status/submission-time index for the committee queue.
2. Create `CommunityMemberConsent`: purpose-specific, versioned consent history
   with account, boolean choice, client source and timestamp; index account,
   purpose and timestamp. It does not contain message content or credentials.
3. Create `CommunityMemberReviewRule`: committee-configured exact-name or
   whole-word review matches, a reason and an active flag. These rules request
   review, never automatically reject unfamiliar names.
4. Add `encrypted_signup_email` (blank text) and `onboarding_version` (integer,
   default zero) to `CommunityChatEmailCodeChallenge`. The encrypted email exists
   only to verify an unknown address before account creation. It is cleared
   after use, invalidation or expiry; the capability version prevents older
   clients from accidentally starting an unsupported signup journey.

There are no deleted columns, account backfills, automatic consent grants,
changes to existing account activation, or production data repair operations.
Previously verified Chat membership is preserved without fabricating an age
attestation or agreement. Approval remains separate from shared account login.

Validation requires creating a disposable local/CI test database with the
existing dependency migration graph and this migration, then running the
onboarding, authentication and account regressions. Production rollout applies
only this new migration to the already-migrated database before enabling
`COMMUNITY_CHAT_SIGNUP_ENABLED`.

Rollback disables new signup and restores the previous clients while retaining
the additive tables and consent history. Pending/rejected applications remain
unable to enrol; disabling signup must never turn them into approved members.
