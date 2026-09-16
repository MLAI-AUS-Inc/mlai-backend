# Daily jobs digest

The backend owns the 07:00 Australia/Melbourne schedule. Roo can request a
manual run; `jobs/services/job_pipeline.py` collects, matches, selects and
publishes jobs. This service does not automate founder matching.

## Slack destination

`JOBS_SLACK_CHANNEL` defaults to `C05QE82M2KE`, the original MLAI jobs channel,
now named `#jobs-matching`. The legacy configured value
`#jobs-and-founder-matching` resolves to that same ID for compatibility with
existing deployments. Other explicit destinations remain unchanged. Prefer
Slack channel IDs in configuration; the bot must be a member of the target.

`#founder-matching` is a separate channel (`C0C230H418U`), not the jobs digest
destination. On the next approved deployment the compatibility alias fixes
existing backend callers without requiring a simultaneous environment edit.
Set the authoritative environment value to `C05QE82M2KE` when maintaining
production configuration. Changing source code does not change live settings
until deployed.

## Outcomes and diagnostics

| Status | Meaning |
| --- | --- |
| `completed` | Selected picks; enabled publishing steps completed |
| `completed_no_results` | No matched listings |
| `completed_no_new_picks` | Matched listings exist, but selection produced no eligible picks |
| `completed_with_publish_errors` | Slack publication failed |

Source outages add `_with_source_errors` to either empty outcome, or produce
`completed_with_source_errors` when picks exist. Empty outcomes are terminal
for the scheduled date, and both Slack and Notion publishing are skipped.
They are not evidence of a Slack failure. Existing historical statuses are
not rewritten.

Selection logs record candidate count, count after publish screening,
historical exclusions, and count after judging. Completion logs record the
run ID, status, fetched/matched/pick counts and whether Slack delivery was
recorded. Screening can also eliminate candidates; do not infer historical
duplicates solely from `completed_no_new_picks`.

Zero-pick outcomes deliberately do not send community messages or failure
alerts. Operators can inspect the persisted run status and counts through
the existing jobs run API. Container logs currently disappear on container
replacement; ship them to retained storage before relying on them for
historical diagnosis.

## TECH-34 remaining recovery work

This first change fixes routing and outcome visibility. TECH-34 remains open
for stale-run recovery, publication-only retry, retained stage diagnostics,
and an operator notification policy. A stale `running` record currently blocks
same-day retries; `completed_with_publish_errors` is also terminal.

Do not blindly retry an abandoned publication or release all historical
unposted selections. Slack may have accepted a message before the process
persisted `slack_posted_at`. Safe recovery needs a durable publication state,
reconciliation for uncertain sends, bounded retries and concurrency fencing.
Historical exclusions intentionally remain based on selection until that
handling exists. No automatic repair or replay is introduced here.

## Validation without migrations

Run `python -m unittest tests.test_jobs_delivery_unit -v` with Python 3.11.
These tests execute delivery seams with mocked dependencies and do not load
Django settings, contact providers, or construct a database. The existing
database-backed scheduler tests require specific migration approval before
execution, as described in the repository README.
