# Optional background website checks

The regular scheduler has one new runner, disabled unless both
MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED and
MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED are on, the founder V2 switch is on,
and website_baseline is explicitly a free setup action. It never charges Roo
points or falls back to a paid path. The existing manual baseline endpoint keeps
its original admission and free-cap behavior through a shared start helper.

The runner visits at most 25 configurations per tick with a fair cursor. It
requires actual first-draft evidence, a current active pinned/legacy owner,
ordinary My startup access, and matching company/organization domain. It reuses
an active website check. With no completed check it can start after the draft;
otherwise it runs once per organization-local date after 02:00, unless a manual
check already completed that day. Invalid timezones fall back to Melbourne.

A private self_heal.website_check_schedule_v1 receipt checkpoints the exact
payload, daily client identity, owner/company/domain and a two-minute nonce lease
before any HTTP call. Competing ticks cannot create a second task. Lost-ack retries
reuse that envelope and key, even if seed keywords change. Three uncertain sends
hold the same key for investigation rather than minting another day's task.
Current ownership, scope, cohort and free eligibility are checked again before
publication; all HTTP happens outside database transactions.

The original rolling per-account free research cap uses idempotent reservations
under account and normalized-account quota locks before HTTP. Automatic checks and
the existing manual free-waiver path share these reservations; historic timestamp
uses count alongside them. A crash between reservation and its receipt retains the
same original slot, and an exhausted allowance starts no work. Confirmed no-work
manual failure releases its slot; unresolved dispatch retains it. Funded manual
admission keeps its existing behavior. The record has no payment/ledger side effects
and does not create points history. Private-only state changes preserve config/run
update clocks and other concurrent JSON data.

A returned BLOCKED run with dispatch_pending_resolution remains pending and keeps
its original key across local dates. A superseded nonce cannot publish. Per-org
journey kill switches are checked even for P2 legacy opt-ins. The scheduler-facing
wrapper returns an explicit skipped/completed/failed status, so a disabled optional
feature does not poison the shared heartbeat.

Enablement remains a rollout decision. No production checks or provider calls
were made by the synthetic acceptance suite.
