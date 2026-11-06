# Mentoring promotion to main

This change brings the mentoring feature from staging onto current main without
the separate staging identity, password sign-in, general Staff Admin panel, or
mobile pilot. Chat's existing CRM `/admin` remains intact. Mentor management lives
at `/mentoring/admin` and follows current main permissions: verified Chat owners
and admins, or active Django staff/superusers. Ordinary members cannot use it.

## Migration review

The files below are copied byte-for-byte from staging, not regenerated. The user
approved these exact two migrations for disposable local/CI test
databases on 4 November 2026. That approval does not cover production. Production
approval is still required for the actual host migration plan.

| Migration | SHA-256 | Effect |
| --- | --- | --- |
| `mentoring.0001_initial` | `f787b567f1fcdbfc11b415d89136d85a33ba003b00935ea67f40b720d97304e9` | Creates mentor profiles, availability rules and sessions. Depends on `roo.0041_apple_iap_digital_credits` and the user model. |
| `mentoring.0002_invites_brief_questions_followup` | `56d7f9dcd9cdd6cff4b25a9fa492f182e0ef541b851e15738a2e42652120ba5a` | Creates mentor invites and adds custom questions, saved answers, meeting claims and application review fields. |

Read-only model and graph checks use `scripts/test_without_database.py --check-models`
and `--describe-migrations`. These do not construct a database or run migrations.
A target operator must inspect `manage.py migrate --plan --noinput`, verify every
pending operation, and compute the plan's approval using
`scripts/validate_website_deploy_config.py --migration-plan`. A file checksum is
not the production plan approval. Never reuse the initial staging plan hash: it
covers only 0001 and does not cover this full rollout.

## Merge and enablement order

1. Review both PRs and complete the approved disposable PostgreSQL booking/ledger
   tests and normal CI checks. The
   existing database-free suites cover the state machine, permissions, points
   calls, scheduling/DST and mocked providers; they do not prove transaction locks
   or real ledger persistence.
2. Obtain separate approval for the actual production migration plan and deploy
   the backend with `MENTOR_BOOKING_ENABLED=false`. Adding the app to
   `INSTALLED_APPS` requires its tables even while its endpoints are disabled.
   Main deploys automatically after its required checks; merging the backend is
   therefore part of the deployment decision. The existing plan gate must pass.
3. Merge the client PR. It makes no mentoring API requests while the home flag is
   absent or false, and hides the sidebar entry until the backend flag is true.
4. Configure the MLAI Google account and perform an attended Meet join test before
   advertising video sessions. Google stays disabled until explicitly configured.
5. Invite a small mentor group, approve their applications, verify availability and
   prices, then enable `MENTOR_BOOKING_ENABLED` for the pilot. Check one request,
   acceptance, pre-start cancellation/refund and completed-session payout.

## Behavior to verify

- A founder must have the session price available when requesting; no points move
  until acceptance. The UI now states this requirement accurately.
- Price and booking answers are copied onto the session. Later edits do not alter it.
- Pending requests expire after 48 hours or at the start, whichever is sooner.
- Cancellation/refund is possible only before the session starts. After its end,
  mentors can complete it. Automatic completion becomes due 24 hours after the end
  and is processed on the next relevant read or bounded mentoring worker pass.
- Calendar checks fail closed for new admissions in both modes. Legacy acceptance
  rechecks conflicts before charging. Instant provider setup has durable recovery,
  exact refunds and unknown-outcome cleanup; see [automatic capture](mentoring-automatic-capture.md).

## Disablement

Resolve charged sessions before a long shutdown. Pre-start sessions may be
cancelled for a refund; already-started sessions cannot be cancelled and need
normal completion or separately reviewed ledger handling. Turning off the flag
also stops lazy settlement, so it does not clear outstanding balances itself.
Retain the additive tables and ledger history. Any schema reversal needs its own
approval and must not run under code that still registers the mentoring app.
