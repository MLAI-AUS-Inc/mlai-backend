# Manual volunteer time

Slack offers `/roo request points` and `/roo award points`. MLAI Chat offers the same actions from Profile, with the review queue and direct awards available to Points Admins. The backend catalogue supplies activity IDs and duration choices to both apps.

The Chat API lives under `/api/v1/community-chat/volunteer/manual-time/`. GET returns the catalogue, the caller’s recent requests or awards and their admin capability. `?mode=review` opens the private review queue. `?mode=members&q=` searches verified Chat profiles for direct awards. POST creates a request for the signed-in account. Provide `target_profile_id` for a direct award. POST to `manual-time/<id>/` approves a pending request.

The Slack service API lives under `/api/v1/points/manual-time/` and requires the strict Roo key. Writes supply the configured workspace and the verified Slack actor. A reviewer reaction must match the recorded review message. Roo claims review and receipt deliveries from the existing durable request outbox.

Submit `catalog_version`, a UUID `submission_key`, one or more `action_ids`, and a positive integer `volunteer_minutes`. The optional `note` accepts up to 500 characters and is required for an unlisted activity. The backend credits 100,000 micro-Roo per minute once. The response gives the exact decimal Roo, status and ledger ID upon approval. The note is available through authorised Chat review. Authorised reviewers can read approved requests with candidate badge IDs using `?mode=badge-candidates`. These records supply the approved ledger and activity IDs for evidence verification.

## Configuration and acceptance

Apply `roo.0050_manual_volunteer_time` before starting the updated backend. It adds two nullable fields and leaves existing requests readable. Set `ROO_MANUAL_TIME_ENABLED=true` after the schema and application are ready. Configure `ROO_MANUAL_TIME_SLACK_WORKSPACE_ID` and `ROO_MANUAL_TIME_SLACK_REVIEW_CHANNEL_ID` on Public Roo. Use the same workspace as `COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID` and an internal private review channel with Roo installed. Verify the bot can open modals, post messages, read conversation history and send DMs in the test workspace.

Try a Slack request approved in Chat and a Chat request approved in Slack. Reconcile each with one ledger entry, an exact balance change, Profile history and the private receipt.

`python -m unittest roo.tests_manual_time_rules` covers pure validation. `python manage.py test roo.tests_manual_time` exercises both directions with Django’s test database and a synthetic service credential. Use PostgreSQL to exercise concurrent approvals with row locking.
