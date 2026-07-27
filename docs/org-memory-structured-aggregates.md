# Organisational memory: Stripe, Xero, and Luma aggregates

## Outcome

PR17 replaces the metadata-only Stripe, Xero, and Luma registrations with
read-only production adapters. The adapters expose an approved, memory-owned
inventory of sanitized metrics and selected Luma event facts. They do not make
raw financial records, customer/contact details, bank information, Luma guest
identities, or registration answers retrievable by Roo.

Provider execution remains fail closed behind the existing deployment and
organisation enablement gates. Landing this code does not enable any provider.

## Data boundary

| Provider | Selectable scopes | Memory content | Explicitly excluded |
|---|---|---|---|
| Stripe | `invoice_revenue`, `cash_collected`, `invoice_count`, `mrr`, `active_subscriptions` | Monthly values, period, currency/count unit, sanitized evidence count, freshness | Invoice descriptions, customers, email, raw Stripe objects, payment methods, credentials |
| Xero | `invoice_revenue`, `cash_collected`, `invoice_count`, `mrr`, `recurring_invoice_count` | Monthly values, period, currency/count unit, sanitized evidence count, freshness | Contacts, invoice/payment payloads, bank accounts, bank transactions, credentials |
| Luma | Exact event IDs plus optional `events_run`, `event_registrations`, `event_attendees`, `event_check_in_rate` | Event name, public URL, date/time, venue, and counts for approved events; monthly selected-event totals | Guest names/emails, phone numbers, registration answers, CSV exports, events outside the selected set, credentials |

The existing integration layer may retain provider payloads for its original
product workflows. The memory adapter never reads those payload fields for
finance. It computes from a narrow sanitized column set on
`ExternalFinancialRecord`: provider, record type, opaque record identity hash,
date, currency, amount, status, and the normalized recurrence category. Raw
record IDs are hashed in the durable calculation inventory and are removed
before a memory source version is produced.

For Luma, guest responses are transient inputs to the existing attendance
counter. Only integer registration/check-in counts cross into the durable
aggregate inventory. The inventory and memory version both carry
`attendee_pii_included=false`.

## Refresh and reconciliation flow

1. The daily memory scheduler wakes every active provider configuration using
   `ORG_MEMORY_SYNC_INTERVAL_SECONDS` (default 86,400 seconds).
2. The structured adapter refreshes the existing provider integration for
   Stripe/Xero or polls the exact approved Luma events.
3. A complete sanitized `StructuredAggregateArtifact` generation is upserted
   for that one connection and its selected scopes.
4. Artifacts absent from the completed generation are marked `removed`.
5. The paginated connector emits immutable source versions and, on its final
   page, reconciles removed/out-of-scope sources.
6. Version metadata produces a deterministic `metric` or `event`
   system-of-record claim with exact chunk evidence. These structured facts do
   not call the general LLM extraction provider.
7. Unchanged values retain the same source revision; a daily refresh advances
   the matching structured claim's `last_confirmed_at` and `stale_after`
   without inventing a new evidence version.
8. A disconnected or inaccessible connection marks its artifacts
   `access_lost` and produces access-revocation removals. It does not silently
   serve the last accessible version.

Current-period facts are marked volatile. Every durable aggregate has a
`stale_after` timestamp (default 25 hours), and connector health reports active
and stale aggregate counts. Webhook and artifact wakes improve latency, but the
daily full poll is the authoritative reconciliation path.

## Wake paths

- Stripe keeps the existing verified Stripe integration webhook. The upstream
  sync writes sanitized financial rows; post-commit signals debounce the
  matching memory configuration. Memory-initiated provider refreshes suppress
  those signals for their own call context so a reconciliation cannot wake
  itself in a loop.
- Xero can call
  `/api/v1/org-memory/webhooks/xero/events`. The receiver verifies the
  base64-encoded HMAC-SHA256 `X-Xero-Signature` against the exact raw body,
  deduplicates the delivery, stores metadata-only receipt information, and
  wakes matching tenant configurations. It never trusts webhook content as
  memory evidence; the adapter performs a provider reconciliation.
- Luma has no webhook dependency. Changes to the existing exact event
  selections cause a debounced wake, and the daily full poll remains the
  fallback.

The Xero endpoint returns HTTP 200 for a valid empty intent-to-receive payload
and HTTP 401 for an invalid signature.

## Configuration

```text
ORG_MEMORY_STRUCTURED_PAGE_SIZE=100
ORG_MEMORY_STRUCTURED_BACKFILL_DAYS=730
ORG_MEMORY_STRUCTURED_STALE_SECONDS=90000
ORG_MEMORY_STRUCTURED_DEBOUNCE_SECONDS=60
ORG_MEMORY_LUMA_TIMEZONE=Australia/Melbourne
ORG_MEMORY_XERO_WEBHOOK_KEY=
```

`ORG_MEMORY_STRUCTURED_BACKFILL_DAYS` bounds invoice/payment history. Active
Stripe subscriptions and Xero repeating invoices remain in the current MRR
calculation even when their creation/start date predates that window.

The Xero webhook key is supplied by Xero when the application webhook is
configured; it is not an OAuth access or refresh token. Configure Xero's
delivery URL as:

```text
https://<backend-host>/api/v1/org-memory/webhooks/xero/events
```

## Operator workflow

1. Apply migrations and deploy the backend/worker/scheduler code together.
2. Leave Stripe, Xero, and Luma absent from `ORG_MEMORY_ENABLED_PROVIDERS`.
3. Configure provider credentials and, for Xero webhooks, the webhook key.
4. Enable one provider for one reviewed organisation through
   `MemoryProviderEnablement` with an approver and approval timestamp.
5. Discover scopes. Finance discovery must show aggregate scopes only. Luma
   discovery may show aggregate scopes and known exact event IDs.
6. Select the minimum required scopes, run preview/dry-run, and approve the
   resulting source configuration through the existing control plane.
7. Run the backfill. Confirm the admin inventory contains only the expected
   aggregate/event facts and no customer, contact, bank, or guest content.
8. Verify connector health after 24 hours, then test disconnect/reconnect and
   deselection reconciliation before expanding the pilot.

## Operational checks

- `StructuredAggregateArtifact` is registered read-only in Django admin.
- An active connector should report `stale_aggregates=0` after its daily run.
- A provider wake receipt must contain only event counts/categories, sequence
  bounds, hashes, and resolved account/scope routing metadata.
- A finance `MemorySource` must always have classification `finance`, even if a
  malformed scope attempted a weaker default classification.
- Source metadata must contain `aggregate_only=true`; Luma sources must also
  contain `attendee_pii_included=false`.
- Removing an aggregate or event scope must tombstone its source on the next
  complete reconciliation. Losing credentials must revoke access instead.

## Verification

The focused tests cover:

- real connector registration and rejection of account-wide finance scopes;
- Stripe/Xero recurrence normalization and historical active-recurring input;
- exact-scope aggregation, pagination cursors, removal, and access revocation;
- Luma event filtering and negative assertions for guest PII;
- stable revisions for unchanged Luma results;
- debounced artifact-save wakes;
- Xero raw-body signature verification, intent validation, replay handling,
  metadata minimization, and tenant routing.

Reference material:

- [Stripe webhook signatures](https://docs.stripe.com/webhooks/signature)
- [Stripe webhooks](https://docs.stripe.com/webhooks)
- [Xero webhooks overview](https://developer.xero.com/documentation/guides/webhooks/overview/)
- [Luma API](https://help.lu.ma/p/luma-api)
- [Luma registration questions](https://help.lu.ma/p/collect-registration-questions)
