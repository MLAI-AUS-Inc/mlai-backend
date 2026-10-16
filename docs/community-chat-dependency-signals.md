# Community Chat dependency signals

This source-level map supports Articles plan G3. A count of 503 responses alone
does not establish which dependency failed in the 13 October incident. The fixed
log fields below distinguish current failure paths without recording request
URLs, file IDs, account/channel identifiers, callback bodies or exception text.

| Endpoint | Dependency and reason | Source and existing response |
| --- | --- | --- |
| `community-chat/link-preview` and `link-preview/image` | `slack`: `single_flight`, `shared_budget`, `upstream_rate_limit`, `upstream_unavailable`, `transport`, or the legacy/default `metadata_pending` | [`slack_file_previews.py`](../community_chat/slack_file_previews.py) distinguishes an overlapping metadata read, shared Slack budget, provider delay and network failure. [`views.py`](../community_chat/views.py) preserves `preview_pending`, 503 and `Retry-After`. |
| `community-chat/slack/users` and other Slack API views | `database`: `database_unavailable` | [`slack_views.py`](../community_chat/slack_views.py) maps a database exception to `slack_storage_unavailable`/503. Slack API and transport failures instead map to 502, and provider budgets/rate limits to 429 with `Retry-After`. |
| `integrations/bridge/buzz/events`, read-cursor callback | `bridge_delivery`: `anchor_pending` | [`inbox_exports.py`](../integrations/services/message_sync/inbox_exports.py) waits for an existing native-message delivery link before assigning a read boundary. This is a retryable dependency wait, represented by `read_cursor_retry`/503. |
| Same read-cursor callback | `relay_adapter`: `adapter_unavailable`; `database`: `database_unavailable`; fallback `read_cursor`: `processing_unavailable` | [`api_views_bridge.py`](../integrations/api_views_bridge.py) classifies adapter and database exceptions; unexpected failures retain a bounded fallback signal and the existing retry response. |
| Same read-cursor callback | `configuration`: `inbox_export_disabled` | A valid signed callback reaches an explicitly disabled `MESSAGE_SYNC_INBOX_READ_EXPORT` path. It retains `inbox_export_disabled`/503 rather than acknowledging an unaccepted cursor. |

Preview waits emit `community_chat_dependency_wait`; cursor waits emit
`community_bridge_dependency_wait`. Expected preview/configuration waits use
INFO; cursor retry failures and Slack database failures use WARNING. Preview
reason strings come from an allowlist and survive scoped negative-cache hits.
The reason is internal observability metadata; API response bodies remain the
existing public-safe contracts. Unsigned callbacks cannot emit a dependency
signal or access a cursor dependency.

## Polling callers

Chat uses the shared account transport and stops timed/focus/reconnect refresh
after final 401/403. Terminal account expiry requests sign-in after its one token
refresh; transient service failures remain recoverable. The website founder-tools
banner formerly started a 25-second `email-draft/active-run` poll without requiring
a selected company. It now pins that company explicitly and stops terminal client
failures; the backend retains its ownership checks and missing-company 400.

The reconciliation-agent capture receiver reloads
`integrations/reconciliation/bank-accounts` through a fresh `BackendClient` before
each refresh batch, with its configured Slack administrator and domain. It now
retains a five-minute 409 cooldown in that persistent receiver, returns 503 and a
decreasing `Retry-After`, and admits no new capture/assist job using a stale
catalogue. This does not cover independent processes directly calling the API.
The backend retains its conflict response when Xero validation is unavailable.

Implementation and synthetic fault tests do not establish deployed client
versions, current production dependencies, or historical incident attribution.
Those require approved runtime evidence. See the
[bridge contract](mlai-chat-bridge-contract.md) for ownership and rollout gates.
