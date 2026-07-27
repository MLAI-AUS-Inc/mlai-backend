# Organisational-memory source control plane

The source control plane lets an authorised Admin Roo operator attach an
existing organisation-owned OAuth connection, select exact source scopes,
preview and dry-run the configuration, approve it, and explicitly request a
backfill. It never makes private source data available to Public Roo.

This began as the PR4 control surface. The included provider adapters intentionally
operate on account metadata only: discovery, preview, and dry-run do not fetch
source bodies or create active memory. The PR6 runtime now durably dispatches
backfill, sync, permission-refresh, deletion, and reprocessing requests, but
content execution still requires the relevant reviewed provider adapter. An
unsupported metadata-only adapter dead-letters explicitly rather than silently
claiming success. Deletion and
access-loss requests synchronously deactivate current chunks through the PR5
evidence kernel before any later physical cleanup runs.

## Prerequisites

1. Apply migrations.
2. Provision an Admin Roo service principal with the `source.manage` scope and
   `admin_roo` surface.
3. Give the acting organisation member the backend-owned `manage_sources`
   capability. Roo cannot submit or override this capability.
4. Create and review source policies in Django admin. A policy records source
   classification, authority, volatility, staleness, allowed memory kinds,
   retention, cutoff, activation, and review rules.
5. Establish the provider's existing OAuth connection in `integrations`.

Every endpoint below uses the signed actor assertion described in
`docs/org-memory-service-identity.md`, resolves the organisation from trusted
backend state, and applies organisation scoping to the connection lookup.

## Safe activation sequence

The API base path is `/api/v1/org-memory/`.

1. Inspect the registry with `GET connectors`.
2. Attach an existing connection with `POST connectors/<provider>/connect` and
   either `external_connection_id` or, for Gmail only, `google_connection_id`.
3. Run `POST connections/<id>/discover`. In PR4 this records bounded account
   metadata only and warns that child-scope discovery is deferred.
4. Select exact scopes with `PUT connections/<id>/scopes`:

   ```json
   {
     "scopes": [
       {
         "scope_type": "folder",
         "external_id": "approved-folder-id",
         "name": "Meeting transcripts",
         "selected": true,
         "classification": "committee",
         "policy_id": 12
       }
     ]
   }
   ```

5. Create an immutable configuration preview with
   `POST connections/<id>/preview`.
6. Run the non-activating validation with
   `POST connections/<id>/dry-run`.
7. Approve the exact current preview with
   `POST connections/<id>/approve` and `{"confirm": true}`.
8. Only after both provider enablement gates and governance approval pass,
   request the backfill using `POST connections/<id>/backfill`, body
   `{"confirm": true}`, and an `Idempotency-Key` header.

Changing a scope, classification, policy, cutoff, retention rule, or other
source configuration invalidates the preview and approval. The worker calls
`validate_action_for_execution()` after claiming and immediately before provider execution, so a
queued request cannot bypass a later change or provider disablement.

Slack direct messages and `no_agent` scopes cannot be selected. Stripe and
Xero accept account/aggregate scopes only. Control-plane metadata rejects
credential-like keys and source bodies and is bounded by depth and size.

## Provider enablement

Provider execution has two independent, fail-closed switches:

1. `ORG_MEMORY_ENABLED_PROVIDERS` is the deployment allowlist. It accepts a
   comma- or space-separated subset of `google_drive`, `slack`, `linear`,
   `notion`, `gmail`, `stripe`, `xero`, and `luma`.
2. **Memory Provider Enablements** in Django admin must contain an enabled,
   human-approved row for the same organisation and provider.

Production also enforces `org_memory/policies/provider_policies.json`. The
current manifest is draft/disabled, so leaving the deployment allowlist empty
is the correct safe default. Validate a deployment with:

```bash
python manage.py validate_org_memory_governance --environment production
```

These gates are checked when the request is created and checked again by the
worker before execution.

## Lifecycle and operations

The normal lifecycle is:

```text
draft -> scoped -> previewed -> dry_run_ready -> approved
      -> backfill_pending -> active
```

Active execution state is owned by the PR6 worker. Runtime endpoints are:

- `POST connections/<id>/sync`
- `POST connections/<id>/reprocess`
- `POST connections/<id>/refresh-permissions`
- `POST connections/<id>/pause`
- `POST connections/<id>/resume`
- `GET connections/<id>/health`
- `DELETE connections/<id>` with `{"confirm": true}` and an
  `Idempotency-Key`

Delete moves the connection to `delete_pending`, removes its selected scopes,
synchronously tombstones associated evidence so it cannot be retrieved, and
queues final physical cleanup for the durable worker. It does not silently
hard-delete audit history.

Daily scheduling, queue operations, retries, and recovery are documented in
`docs/org-memory-runtime.md`.

All configuration, state changes, previews, dry-runs, approvals, and action
requests create organisation-scoped audit records. Preview and action records
are read-only in Django admin; edits to policies, scopes, or connection
configuration invalidate current approval and create an admin audit event.
