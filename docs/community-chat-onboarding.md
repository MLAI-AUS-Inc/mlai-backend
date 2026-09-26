# Private community onboarding

Source contract, 21 September 2026. Requires approval, creation and application
of `community_chat.0012_member_onboarding`; see
[the migration proposal](community-chat-onboarding-migration.md). This document
does not establish production readiness or a completed deployment.

## Account proof and admission

Set `COMMUNITY_CHAT_SIGNUP_ENABLED=true` only after the additive migration,
client rollout, disposable-database checks and email-provider validation.
The default is false. Supported clients send `onboarding_version: 1` in the
existing email-code request. For an unknown eligible email, an encrypted
short-lived recipient is stored on the challenge and a transactional code is
queued. No user is created before verification. The ciphertext is cleared on
consumption, resend invalidation, terminal failure or expiry cleanup.

A valid proof creates or reuses the case-insensitive canonical account, with
no usable password for new accounts. It issues an installation-scoped Chat
session. An applicant receives `status: onboarding_required`, an empty
`bootstrap_token`, and private `onboarding` state. A pending, rejected or
suspended application cannot bootstrap/enrol, authorize a desktop installation,
update a public profile or call protected community APIs. Own-account reads,
onboarding, privacy/deletion requests and sign-out remain available.

Previously verified Chat users retain membership without invented consent.
An active shared MLAI account alone does not prove community admission while
signup is enabled. Existing application decisions remain enforced when the
signup flag is disabled. The flag is not a suspension/revocation mechanism.

## GET/PUT /api/v1/community-chat/account/onboarding/

Authentication is the existing Chat account session; bootstrap credentials
cannot submit answers. Exact-origin validation applies to cookie mutations.
The server derives the owner from the session and revalidates the user and
session under the existing user-first lock order before every write.

GET returns `{onboarding, profile}` with `Cache-Control: no-store`.
`onboarding` contains `available`, `required`, `status`, `basics_complete`,
the current `policy_version`, canonical policy URLs, own email/name, optional
preferences and the allowed city/interest choices. It contains no review notes
or other applicants' details. Account GET includes the same private state.

PUT accepts one of two steps:

```json
{"step":"basics","first_name":"Alex","last_name":"","adult_confirmed":true,"accept_rules":true,"policy_version":"2026-09-21"}
```

First name is required, surname optional; each is limited to 80 characters.
Eligibility and agreement must be JSON `true`, and the version must match the
current policy. Three separate consent-purpose records track adulthood, terms
and the conduct rules in the canonical terms. No DOB is collected. Repeating
the same decision/version is idempotent.

```json
{"step":"complete","city":"Melbourne","interests":["research"],"marketing_opt_in":false}
```

City and interests are optional; zero to three unique supported interest IDs
are allowed. Marketing is independent and uses an explicit boolean. Omission
does not grant permission. Sending `{"step":"complete","skip_personalisation":true}`
preserves previously saved preferences and ignores all optional edits. Unknown
fields, including owner IDs and admission status, are rejected.

New members must complete basics before submission. Exact reserved official
names, obvious links and active committee-defined exact/whole-word rules send
applications to review. Familiarity, short names, ethnicity and punctuation
are not rejection rules. Ordinary submissions approve automatically. Only
approved names are copied into the canonical public profile. Existing members
edit optional preferences without repeating signup, and public-name changes
continue through the versioned profile API with impersonation checks.

## Committee review and consent

Django admin provides a permissioned queue with status/city filters and name or
email search. Staff with change permission can approve eligible pending
applications, request correction or reject them. Actions recheck eligibility
under user/profile locks and record the reviewer/time plus an admin audit log.
Consent evidence is read-only in admin. Members can correct a pending name and
resubmit. No review deadline, automatic status email or marketing campaign is
promised or triggered.

The `suspended` state is enforced by the account gate, but this release does
not add an admin suspension workflow. Existing device/relay revocation must
still be used when removing a previously admitted member; changing a profile
status alone does not remove an already-issued relay membership.

Marketing history distinguishes not asked, no and yes. Profile edits record
withdrawal, but this release creates no Customer.io marketing campaign or
subscription synchronization. Future sends must use current purpose-specific
permission and existing suppression/unsubscribe state. AI-sharing consent is
unchanged and separate. Private onboarding answers are never public metadata.

## Rollback and validation

Disable signup to pause new applicants and preference writes. Preserve the
tables and audit history; do not backfill acceptance or erase applications.
Existing session/device authorization and rejected/pending decisions remain
enforced. Keep deployment disabled until the exact migration and disposable
test migration closure are approved.

Run `community_chat.tests.test_onboarding` with the existing email-code,
desktop-auth, account-session, account-profile, privacy, bootstrap and Slack
regressions. PostgreSQL concurrency checks must cover simultaneous email
verification and application submission. Exercise new signup, skip, resume,
review/correction, old-client sign-in and cross-device approval against the
deployed backend before enabling general signup.
