# Managed MLAI Chat channels

The integrations worker tracks one shared discussion UUID per project or event. The relay enforces room visibility and publishes managed metadata. A change in provider title, schedule or business status updates the channel details without posting a message. Historical conversations keep their original UUID. The Slack member bridge remains responsible for message synchronization and catalog access.

## Configure a source

Set `MANAGED_CHANNEL_SOURCES` to a JSON object in the web and worker environments. The following example contains placeholders for identities an operator must verify. `enabled` permits receipt intake and source reads. `write_chat`, `write_backlinks` and `slack_events` independently enable delivery to a destination. Use one installation per combination of relay community, provider and provider workspace or calendar.

```json
{
  "community-calendar": {
    "enabled": true, "provider": "luma", "scope": "<MLAI calendar ID>",
    "community": "<relay community UUID>", "public_event_rooms": true,
    "public_external_listings": false, "write_chat": false, "slack_events": false
  },
  "linear-projects": {
    "enabled": true, "provider": "linear", "scope": "<Linear workspace UUID>",
    "community": "<relay community UUID>", "team_ids": ["<Linear team UUID>"],
    "public_project_ids": ["<reviewed Linear project UUID>"],
    "write_chat": false, "write_backlinks": false
  },
  "plane-projects": {
    "enabled": true, "provider": "plane", "scope": "<Plane workspace UUID>",
    "community": "<relay community UUID>", "workspace_slug": "<Plane workspace slug>",
    "api_origin": "https://plane.mlai.au", "public_origin": "https://plane.mlai.au",
    "linear_scope": "<Linear workspace UUID>", "linear_project_map_path": "<mounted checkpoint>",
    "public_project_ids": ["<reviewed Plane project UUID>"], "write_chat": false
  }
}
```

The project adapter also accepts `public_team_ids` with `full_team_visibility_verified: true` or Plane's `public_workspace_projects: true` for future projects within a verified public source policy. Set `bindings` by external resource ID to adopt a positively identified shared `channel_id`. Use `private_rosters` to map individual source IDs to authorized Chat device public keys. Every configured key must still resolve to an active verified device. The worker withholds a private projection until it can prove the audience. Source membership, attendees and Slack linking consent are independent.

For a Linear project with a Slack identity, configure `slack_workspace_id` and the bridge destination's verified `relay_workspace`. Only a current shared mirror can qualify as a primary room. Personal mirrors remain grant-scoped. For private imports, configure `MANAGED_CHANNEL_SLACK_SECTIONS` as a JSON object keyed first by Slack workspace ID and then by conversation ID, with values `committee`, `project` or `event`. These mappings can preserve a curated section across a Slack rename. The existing MLAI Slack workspace also recognizes the initial exact channel names in its authorized private catalogs.

The Luma adapter verifies the server key's calendar identity and reads approved native and external listings. Public rooms require an explicit audience decision. External listings lack a source visibility field and require `public_external_listings` or a verified private roster. Collection and expiry use UTC instants while projection retains the event timezone. Cancellation is retained on a durable source reference. Previously approved events confirmed absent from the calendar become Removed.

Linear-to-Plane mirrors must carry source provenance that matches the import checkpoint. To cut over a verified mirror, add its Plane project ID to the Plane installation's `authoritative_project_ids`. Both provider references stay attached to the same entity. Neither names nor similar URLs qualify as identity evidence. Missing permission triggers revalidation. Signed removal plus targeted not-found can retire a project. A full scan omitting a project alone cannot prove deletion. Linear backlinks use an integration-owned project resource. The installed Plane fork has no supported project resource writer.

## Classify an existing shared room

`python manage.py classify_chat_channel --community <uuid> --room <existing-uuid> --section committee --visibility public` previews the scoped registry change. Add `--apply` to publish the section to the relay. For private rooms, supply each authorized verified public key with `--member`; the relay must confirm the exact existing audience. The command rejects personal mirrors and provider-owned rooms. A shared curated room keeps its sidebar section when an operator explicitly promotes it to a provider primary via an exact `bindings` entry containing `adopt_curated: true` and its UUID. The native title remains visible until the provider owns its projection.

## Intake and worker

Configure each provider's verified webhook destination at `POST /api/v1/integrations/managed-channels/webhooks/<installation>`. Configure a different secret per installation using `webhook_secret_env`. The defaults are `MANAGED_CHANNEL_LUMA_WEBHOOK_SECRET`, `MANAGED_CHANNEL_LINEAR_WEBHOOK_SECRET`, and `MANAGED_CHANNEL_PLANE_WEBHOOK_SECRET`. The worker uses `api_key_env` or the corresponding `LUMA_API_KEY`, `LINEAR_API_KEY` or `PLANE_API_KEY`. For self-hosted Plane behind Cloudflare Access, `cf_access_client_id_env` and `cf_access_client_secret_env` select the credentials. Never put credential values into the JSON settings or audit output.

Use the existing configured relay URL, a dedicated `MANAGED_CHANNEL_RELAY_SIGNING_KEY` on the worker, and the corresponding `MANAGED_CHANNEL_RELAY_COMMUNITY_ID`. Set the corresponding `BUZZ_MANAGED_CHANNEL_PUBKEY` on the relay and give the signing identity community administrator authority. Set `MANAGED_CHANNEL_WORKER_ENABLED=true` in the deployment configuration to supervise `managed-channel-worker`. It runs independently of the shared discovery scheduler, repairs provider inventories every fifteen minutes, ticks event transitions, and retries due inbound and outbound work. The web process needs webhook verification secrets. The relay signing key and optional Slack management token can live in the worker-only `.managed-channel-worker.env`.

`python manage.py sync_managed_channels --installation <name>` reads and compares the complete provider inventory. Add `--apply` to reconcile that enabled source immediately. With source writers paused, the registry and desired deliveries can be inspected without changing Chat, Linear or Slack. `ManagedChannelScope.scanned_at` records the last complete inventory. `ManagedReceipt.error` and `ManagedDelivery.error` record bounded failure reasons and the next eligible retry. A transient source failure preserves the previous entity state.

## Optional Slack event conversations

Set `slack_events`, `slack_workspace_id` and optionally `slack_bot_token_env` on the Luma installation. The default token name is `MANAGED_CHANNEL_SLACK_BOT_TOKEN`. Use `sync_managed_channels --installation <name> --slack-capabilities` for a read-only check of the workspace bot's identity and public channel management scope. The worker repeats the check before each operation. Insufficient permissions hold only the Slack delivery. Current automatic Slack creation covers newly provisioned public discussions. Restricted audiences and adopted channels need separate verified ownership. Existing Slack message readers, private imports, and membership grants retain their existing authority.

The worker reserves the channel handle before calling Slack. Member-channel discovery consults this reservation to avoid a second Chat room. An explicit name collision can use a stable suffix; an unknown network result retains its reservation and requires a positively verified Slack ID before another create. Renaming addresses the stored Slack ID. Setting `slack_archive_removed` also permits archival on confirmed removal and unarchival on restoration. An event's scheduled end by itself leaves the conversation available. To pause external changes, turn off the corresponding writer. Retain the registry and reservations across rollback.

## Verify in isolation

The PostgreSQL migration is `integrations.0052_managed_channel_sections`.
Run the focused provider and persistence tests with a disposable Django
database. The separate `integrations.tests_managed_channel_sync_e2e` scenario
uses signed provider fixtures at the HTTP boundary and a running loopback
Buzz relay. Seed the relay's disposable database with its deployment community,
bootstrap a test owner as the managed writer, and trust the local TLS test
certificate using `REQUESTS_CA_BUNDLE`. Set `MANAGED_CHANNEL_E2E_RELAY_URL`
to the relay's HTTPS loopback URL, `MANAGED_CHANNEL_E2E_COMMUNITY` to the seeded
community UUID, and `MANAGED_CHANNEL_E2E_RELAY_KEY` to the test owner's signing
key. The test rejects external network destinations.

```sh
python manage.py test integrations.tests_managed_event_channels integrations.tests_managed_channel_projects integrations.tests_managed_channel_relay_client integrations.tests_managed_channel_sync_e2e
```
