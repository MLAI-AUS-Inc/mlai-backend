# Verified availability and automatic mentoring meetings

This change fixes personal-calendar protection for legacy mentors and extends the
existing instant-booking worker with required transcription and Gemini notes.
It does not enable mentors or change historical sessions through a data migration.

## Booking contract

- All selected personal conflict calendars are read in both modes. Personal
  connection history prevents fallback to the old organizer after disconnect.
  A missing/failed check returns no selectable slots. Availability exposes additive
  `availability_status`; only `busy_check: ok` is actionable on older responses.
- Legacy request and acceptance preflight external conflicts before entering row
  locks, then fence all connection revisions/profile revision and a maximum
  ten-second evidence age. A conflict blocks writes/charges. Existing accepted
  replays and cancellation/refund operations do not require a new check.
- New requests use saved buffers/notice/increment policies and participant
  reservations. Existing session buffers remain frozen.
- Automatic bookings debit once and enter provisioning without mentor acceptance.
  The worker recovers one deterministic Calendar event and conference, configures
  the exact Meet space, GETs both required settings, rechecks conflicts, and only
  then confirms. Invitations use the existing durable retry operation. A failed
  required setup never silently downgrades to a meeting without capture.
- Unknown provider writes retain their cleanup tombstone and reservation. Refunds
  remain exactly once. Provider calls remain outside row locks.

## Capture and permissions

Profile `meeting_artifact_policy` accepts `{}` or
`{"version":1,"transcription":"required","smart_notes":"required"}`. A first
explicit activation defaults to the latter. Policy is signed in the quote and
frozen in `booking_snapshot`; provider results live separately in
`meeting_provisioning`. Editing the policy increments booking revision.
Participant session responses expose only `capture_enabled`, not the Meet space
ID. The quote exposes `capture_required` for disclosure before payment.

The OAuth consent requests `https://www.googleapis.com/auth/meetings.space.settings`
in addition to existing Calendar permissions. Existing grants require reconnect.
The permission sets are separate so free/busy protection keeps working without
Meet settings consent. Refresh responses that narrow scopes update stored grants.

After Calendar conference creation, the adapter resolves `spaces/{meetingCode}`
to the canonical space name, validates its meeting URI, and patches only
`config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration` and
`config.artifactConfig.smartNotesConfig.autoSmartNotesGeneration` to `ON`.
It reads them back before confirmation. Video recording, moderation, access and
Drive sharing are untouched. The same grant belongs to the meeting organizer.
See [Google's configuration guide](https://developers.google.com/workspace/meet/api/guides/meeting-spaces-configuration)
and [space resource](https://developers.google.com/workspace/meet/api/reference/rest/v2/spaces).

An enabled setting is not proof that a transcript already exists. Validate actual
Workspace licensing, admin controls and storage in an attended pilot. Automatic
capture requires eligible host participation; Google documents web host/co-host
joining for automatic transcription. Preserve Meet's notices, consent and stop
controls. Verify output artifacts and actual sharing after the test call. Chat
neither ingests transcripts nor automatically broadens artifact access.
See [transcription requirements](https://support.google.com/meet/answer/12849897)
and [Gemini notes](https://support.google.com/meet/answer/14754931).

## Release and Sam's activation

1. Review `mentoring.0004_meeting_capture` and the migration proposal. Only local
   generation/disposable-test application is approved. Before production, inspect
   the actual pending graph and obtain approval for the exact production plan.
2. Deploy backend/schema and the companion Chat UI in the normal release flow.
   Confirm Calendar/Meet APIs, OAuth consent configuration, encryption, shared
   cache, PostgreSQL and `process_mentor_bookings` scheduler health. Keep existing
   recovery work running. Never disable the parent mentoring flag as rollback.
3. Sam reconnects the existing work Google identity to grant Meet settings access.
   Preserve selected work/personal conflict calendars and `sam@mlai.au` destination.
   Confirm the organizer has an eligible Workspace account and admin permissions.
4. Inspect `configure_mentor_automatic_bookings 1 --expected-email sam@mlai.au`.
   Its read-only default shows readiness. The separately authorised `--apply`
   invocation enables automatic booking and both capture defaults through the
   normal settings service, preserving saved price/hours/notice/buffers. It fails
   on identity mismatch or missing permission. Do not bulk-enable other mentors.
5. Read guest availability for 9–13 November. Monday's confirmed 09:00–18:00 Busy
   event excludes all offered hours. Compare every protected slot across both
   calendars, including recurring Deep Work blocks. Unknown verification offers
   no times. Confirm a genuinely free interval remains available.
6. In an authorised pilot, book one free slot without mentor acceptance. Verify
   the single work-calendar event, matching Meet URL, attendee invitation, exactly
   one charge, and both settings ON. Join as the eligible host on web, observe
   capture starting without manual toggles, and verify both output artifacts after
   the call. Mocked tests do not establish live Google capability.

Production release, account consent, actual activation and the live pilot are
separate operational acceptance work; they have not been performed by local tests.

## Existing confirmed session missing its link

The scheduled booking worker automatically admits future legacy video sessions
that were skipped with `unavailable/google_off` once their mentor uses instant
booking with the saved required transcription/notes policy. Recovery never opts
a mentor into capture. Admission requires no event ID, calendar target, Meet link, provider
path or provisioning state: this is positive evidence that the old provider was
never invoked, not an inference from a Google 404. The worker verifies the
mentor's selected personal calendar, rechecks eligibility under participant
locks, then uses the same deterministic repair and required capture verification.
No appointment terms or ledger entries change. Readiness failures are logged by
safe code and retried after five minutes, with oldest unattempted work first and
the existing per-tick count/time budgets. Known or uncertain old provider writes
still require the explicit procedure below; they are never moved to a guessed
organizer. New instant bookings continue to confirm only after setup succeeds.

`repair_mentor_meeting <session-id>` inspects one session without changing it.
Only an explicitly authorised `--apply --connection-id <id> --calendar-id <id>`
prepares a repair. Validate the exact screenshot session (11 November, 16:00 AEDT)
and its provider/ledger state first. The command verifies the supplied target,
refuses to move a known legacy event to a guessed organizer, and refuses past or
non-video/non-accepted sessions. Known legacy organizer identities need separate
reconciliation rather than an automatic second event.

The durable repair state lives on the original session, uses its deterministic
Calendar identity, and is retried by the mentoring worker/owner Retry action.
The operation never invokes booking/accept/charge or changes frozen appointment
terms. A cancellation stops confirmation, cleans the same event and retains time
when a remote insert is unresolved. In-flight invitations are followed by Google
cancellation updates where necessary. A repair can fail while the original
appointment remains accepted; show setup needs attention rather than claiming a
usable video meeting. Do not bulk-rewrite other sessions.

## Validation and observability

Run the assigned database-free mentoring suites and approved PostgreSQL booking
suites with synthetic providers. Exercise configuration revision races, two
founders choosing one slot, lost insert/PATCH responses, capture read-back OFF,
permission failure/refund, cancellation and legacy repair. Frontend checks cover
status parsing, stale-query failures, consent disclosure and the setup/confirmed
flow. Never log provider bodies, tokens, event content or private briefs.

Existing worker logs use safe attempt IDs/error codes. Monitor oldest due setup,
failed capture, invitation retries, refund and cleanup outcomes. Inspect persisted
verification status during a pilot; do not infer it from a success HTTP response
or an OAuth readiness badge alone.
