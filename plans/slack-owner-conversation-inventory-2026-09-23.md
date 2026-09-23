# Owner Slack conversation inventory: proposed migration and API

Status: **proposal for review; no migration has been created or applied**.
The backend `AGENTS.md` requires explicit user approval for a specific migration
before creating, running, or applying it. The proposed migration is
`integrations.0048_owner_slack_conversation_inventory`, based on current
`origin/main` migration `0047_message_sync_reliability`. Confirm the dependency
again immediately before implementation.

## Problem and boundary

`users.conversations` discovers private channels, group DMs, and 1:1 DMs in
20-item pages, but `discover_conversations()` skips new conversations outside
the selected 7/30-day message window before storing a mirror. Separately,
public channels only appear in MLAI Chat when an operator has approved a
shared bridge mapping. The existing `channel_catalog` contains only
provisioned relay rooms and retired ID fences.
Clients hide mirrors until their selected archive scan and delivery qualify
`ready_for_display`. Thus a conversation can exist in the user's Slack account
without a row in MLAI Chat even when the importer is operating correctly.

This inventory is **metadata-only** and owner-only. The existing grant and
verified-device checks remain the access boundary. The selected history window
continues to govern message import and publication; an inventory row never
creates a relay room or grants another Slack participant access. The inventory
covers `im`, `mpim`, `private_channel`, and joined `public_channel` metadata from
the connecting user's Slack account. Public channel **content** remains
governed by the separately approved community bridge manifest. An unmapped
public row is visible only to its owner and cannot autojoin, mirror, or publish
to the shared community. Slack Connect content stays unsupported.

The connection/consent copy should state that conversation names and source
activity may be listed outside the selected message-history window. If the
current bounded consent does not permit that metadata scope, obtain the
member's explicit choice before filling older inventory rows.

## Proposed table

`SlackOwnerConversationInventory` in `integrations/models.py`:

| Field | Type and purpose |
| --- | --- |
| `id` | `BigAutoField` primary key |
| `grant` | `ForeignKey(SlackDmMirrorGrant, on_delete=CASCADE)`; owner, OAuth connection and consent epoch are resolved through the grant |
| `slack_conversation_id` | `CharField(max_length=100)`; source identity, never a relay channel ID |
| `kind` | `CharField(max_length=24)` with `im`, `mpim`, `private_channel`, `public_channel` choices |
| `source_name` | `CharField(max_length=255, blank=True)`; Slack's supplied channel/group label; no message text |
| `counterpart_slack_user_id` | `CharField(max_length=100, blank=True)` for an IM; source user ID only |
| `display_name` | `CharField(max_length=255, blank=True)`; optional profile-derived row label, resolved under the existing user token and request budget |
| `source_activity_ts` | `CharField(max_length=32, blank=True)`; latest proven source message timestamp, never Slack's channel `updated` value |
| `source_archived` | `BooleanField(default=False)` |
| `source_is_open` | `BooleanField(null=True)`; preserve unknown when Slack omits this DM/MPIM flag |
| `eligibility` | `CharField(max_length=24)` with `eligible`, `unsupported_external`, `unsupported_ambiguous`, `permission_limited` choices; this controls display only, never delivery authorization |
| `last_seen_sweep_id` | `UUIDField(null=True)`; assigned when a source page is accepted under its exact grant/OAuth/consent epoch |
| `first_seen_at`, `last_seen_at` | UTC timestamps for freshness and audit; no message content |

Constraints and indexes:

- Unique `(grant, slack_conversation_id)`; source IDs can recur across
  workspaces, so a source ID alone is never a key.
- Index `(grant, kind, source_archived, slack_conversation_id)` for stable
  keyset pagination, plus `(grant, last_seen_sweep_id)` for full-sweep cleanup.
- Do not duplicate access tokens, message bodies, full participant sets, read
  cursors, or relay key material in this table. Existing conversation and
  read-state records retain those responsibilities.

The migration is schema-only: create the table and indexes, with **no data
backfill, Slack calls, or destructive operations**. A later worker pass fills
it under the current grant. Disconnect/revocation deletes the grant, cascading
to inventory rows; pause hides names from the API without deleting them.
Account deletion follows the same cascade. A changed grant consent or Slack
identity hides source names until the current authorization is revalidated.
Routine OAuth token rotation for the same workspace and user may leave prior
rows visible as explicitly stale while a fresh sweep runs; it must not
repeatedly empty the owner's directory.

## Discovery and reconciliation

Sweep `im,mpim,private_channel` and joined `public_channel` types with separate
coverage checkpoints because their Slack scopes differ. Request
`exclude_archived=false` for the metadata sweep so archived joined rows are
visible with their archived flag; keep the importer's existing archive and
message-window rules. Public enumeration
requires `channels:read`, and missing permission yields
`coverage.public_channel="permission_required"`, never an empty complete
directory. Missing `mpim:read` or `groups:read` likewise marks that specific
kind permission-limited rather than complete.
At each `users.conversations` page, validate source IDs, type, sharing flags,
archived state, and metadata before the current recency/history gate. Upsert
only after `_lock_slack_grant_api_authority()` confirms the exact grant,
workspace, Slack user, consent generation and OAuth generation captured for
that request. Keep the existing 20-item page checkpoint and shared Slack API
budget. `conversations.info`, `users.list`, or `users.info` can enrich activity
and display labels later under the same budget; missing hints remain unknown.

Allocate one sweep UUID per type group in the connection's existing
`sync_cursor` checkpoint, along with page cursor and the exact grant, workspace,
Slack user, OAuth generation, and consent generation. Store a compact
`owner_inventory_sweep` summary there with completion time, source identity,
and coverage per kind; do not add a second sweep table. On a complete source
listing, publish that group's successful sweep marker and retire its rows not seen in that
sweep only after all pages succeeded. A 429, unexpected page, process restart,
or per-conversation error leaves previous rows marked stale, not absent.
Preserve the latest successful name/activity until revalidated; expose its
observation time. Source event callbacks may update an existing row but cannot
prove the whole inventory complete. Never infer message deletion from
inventory absence. Public listing is owner metadata only; matching it to an
existing approved bridge is a read-only lookup.

External and pending external shares may be represented by ID and source label
as `unsupported` for the owner if product policy approves that metadata
visibility. They must never be provisioned or delivered. Ambiguous sharing
remains unsupported until Slack explicitly classifies the audience. The
existing `_is_external_shared_conversation` policy is the content gate.

## Owner-only API contract

`GET /api/v1/community-chat/slack/conversations/?cursor=<opaque>&limit=50`
in `community_chat/slack_views.py` and `community_chat/urls.py`. Clamp the limit
to `1..100`; use a signed, versioned keyset cursor bound to grant ID, workspace,
Slack user, OAuth/consent generation, and filter/sort key. A stale or foreign
cursor returns a validation error without leaking rows. Set
`Cache-Control: private, no-store`.

Require an authenticated account, active matching grant, connected Slack
connection, and a currently verified `community_chat_public_key` belonging to
the owner. A missing/unverified device gets an error, not an empty response.
Revalidate on every page. Never expose inventory through community/relay
directory queries or the public bridge. A paused, revoked, disconnected, or
identity-mismatched grant returns no source names.

```json
{
  "items": [
    {
      "slack_conversation_id": "D0123456789",
      "kind": "im",
      "name": "A person",
      "last_message_at": "2026-09-23T00:00:00Z",
      "source_archived": false,
      "state": "source_only",
      "mlai_channel_id": null,
      "read_state": {
        "availability": "unknown",
        "is_unread": null,
        "unread_count": null,
        "has_personal_mention": null,
        "observed_at": null
      }
    }
  ],
  "next_cursor": null,
  "total": 1,
  "eligible_total": 1,
  "discovery_complete": false,
  "last_sweep_at": null,
  "coverage": {
    "im": "pending",
    "mpim": "pending",
    "private_channel": "pending",
    "public_channel": "pending"
  }
}
```

`total` counts all source rows for this grant, including archived and
unsupported rows, both personal and joined public; it is provisional while
`discovery_complete` is false. `eligible_total` excludes unsupported rows.
Private/DM states are derived from inventory plus current mirror and publication
records: `source_only`, `importing`, `ready`, `out_of_window`, `error`,
`unsupported`. A private/DM row receives a relay UUID only when the current
owner/device is provisioned for that mirror; `ready` additionally requires
existing `ready_for_display`. Public rows use `mapped` or `unmapped`: `mapped`
requires an enabled `CommunityBridgeChannel` with the same Slack workspace and
channel ID, `destination_platform="buzz"`, and a nonempty valid destination UUID.
The mapping is looked up, never created, by this endpoint. The frontend still
checks whether that relay room is visible before routing to it. An unmapped
public or source-only private row never masquerades as an MLAI `Channel` or
uses a fabricated UUID. Page responses distinguish stale inventory from a
fresh complete sweep; each `coverage` kind reports `complete`, `pending`,
`stale`, or `permission_required` as applicable. `discovery_complete=true`
only when all four kinds are complete under the current grant and identity.

Unread state is keyed internally by Slack ID. Extend the current read-state
worker's target sweep to include authorized inventory IDs even before a relay
room exists. Build `ReadTarget(channel_id=slack_id, slack_id=slack_id, kind=kind,
conversation=inventory_row)` so the existing cache key, Slack scope checks,
rate budget, and selected history-window clamp apply. Run this as a separate
bounded source-read sweep, prioritizing IM snapshots so Slack's authoritative
`unread_count_display` identifies currently unread 1:1 DMs before archive
delivery finishes. Pagination of metadata must never wait for 50 Slack
`conversations.info` or history calls. The inventory API reads that cache and
projects only `is_unread`, count, personal mention flag, observation time and
availability. It never stores message bodies in the new table or caches them
in the API. An unavailable field stays `null`, never zero. A cached value
older than the published freshness threshold is `stale`. A source-only row
with `is_unread=true` participates in the owner's Catch up list, where it
remains visibly unopenable until existing consent and provision gates produce
a ready room. Do not derive unread state from latest-message time or a read
cursor, and do not infer Slack thread notifications from channel snapshots.
Unread badges can differ from raw message counts for channels, so Catch up uses
the explicit `is_unread` boolean, not `unread_count > 0`. For MPIM and channel
rows, bounded history, Slack pagination, or missing `last_read` can leave the
count or unread flag unknown; surface that uncertainty instead of promising
exact live parity for data Slack has not supplied within the user's consent.

An eventual `POST /api/v1/community-chat/slack/conversations/open/` accepts a
source ID, rechecks current source membership, kind, sharing and grant/device
authority, then uses the existing provision path only for messages allowed by
the selected history window. An out-of-window row stays visible with an
explicit all-history consent action; opening it does not expand consent.
Recipient-based `POST /slack/dms/` remains separate because it may create a
new Slack DM. Frontends should render source-only rows with state and without
a channel route until a current relay UUID is ready.

## Rollout, rollback and tests

1. Review and explicitly approve the exact migration. Generate it from the
   agreed model, inspect SQL and the migration plan, then apply only through
   the reviewed deployment procedure. The API/worker must tolerate an empty
   inventory during rollout and keep existing `channel_catalog` behavior.
2. Enable metadata writes behind a backend feature flag, then validate a
   controlled account against Slack source IDs across all pages. Only then
   expose the endpoint to web/mobile; retain the current import UI as fallback
   when inventory is unavailable.
3. If rolled back, stop writes and hide the endpoint; retain the table and its
   rows for a later reviewed cleanup. Do not reverse/drop a used table as a
   routine rollback. Grant revocation/account deletion still purge rows.
4. Test paged source discovery, process restart/429, same-second activity,
   empty/partial pages, per-row failure, archived/public/private/group/IM flags,
   internal versus external sharing, consent/OAuth switch, device revoke,
   owner isolation, disconnect cleanup, stale/foreign cursor, source-only
   unread/Catch up inclusion, missing public scope, mapped versus unmapped
   public rows, and source-only open attempts. Database tests must use a
   disposable approved migration setup; no production data or credentials are
   needed.

The existing public bridge remains a separate policy decision. The owner-only
public source inventory compares the user's joined Slack channels with that
approved mapping, without creating community channels or publishing content
because a source ID appears in Slack.
