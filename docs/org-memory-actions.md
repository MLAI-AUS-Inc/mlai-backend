# Controlled Admin Roo actions

PR21 adds a fail-closed proposal, approval, execution, reversal, and audit
gateway for a deliberately small set of Admin Roo actions. It does not give a
model general-purpose connector credentials and it does not enable autonomous
writes.

The gateway is disabled by default. Public Roo cannot authenticate to it.

## Binding safety boundary

Memory can inform a proposal but is never the sole authority for an external
mutation. The gateway performs this sequence:

```text
verified Admin Roo actor
→ allowlisted, schema-validated proposal
→ authorised evidence validation
→ live provider precondition read
→ immutable proposal + content-minimised audit
→ independent approval when required
→ second live precondition read
→ stale approval invalidation or idempotent execution
→ durable result and reversal receipt
→ provider-result ingestion request
```

The initial allowlist is:

| Action | Risk | Approval | External mutation |
| --- | --- | --- | --- |
| `draft_gmail` | low | no | no; local draft only |
| `draft_slack_post` | low | no | no; local draft only |
| `draft_notion_update` | low | no | no; local draft only |
| `create_linear_issue` | medium | yes | yes |
| `update_linear_issue` | high | yes | yes |

Sending email, posting Slack messages, changing canonical Notion content,
calendar invitations, public publication, contracts, sponsor commitments,
payments, finance records, roles, and governance are not action types in this
gateway. Unknown action types and unknown payload fields fail closed.

## Identity and authorization

Every endpoint requires:

- an active service principal with `allowed_surfaces=["admin_roo"]`;
- the `org_memory.actions` service scope;
- a signed, single-use Admin Roo acting-user assertion;
- active organisation membership; and
- `view_general_memory`.

Approval, rejection, and reversal additionally require `approve_actions`.
Users without that capability can see only their own proposals. The safe
default requires an approver other than the proposer.

The organization always comes from the authenticated principal and verified
actor. A request cannot select another organization. Public Roo credentials
are rejected by private-memory authentication before the action view runs.

## API

```text
GET|POST /api/v1/org-memory/actions
GET      /api/v1/org-memory/actions/{proposal_id}
POST     /api/v1/org-memory/actions/{proposal_id}/approve
POST     /api/v1/org-memory/actions/{proposal_id}/reject
POST     /api/v1/org-memory/actions/{proposal_id}/execute
POST     /api/v1/org-memory/actions/{proposal_id}/reverse
```

All mutating requests require an `Idempotency-Key` containing 8–255 safe
characters. Proposal replay returns the original proposal only when the
canonical request hash matches. Approval, execution, rejection, and reversal
replays are also stable. Reusing a key for different content is rejected.

Example Linear proposal:

```json
{
  "action_type": "create_linear_issue",
  "configuration_id": "00000000-0000-0000-0000-000000000000",
  "input_payload": {
    "team_id": "linear-team-id",
    "project_id": "linear-project-id",
    "title": "Confirm venue accessibility"
  },
  "evidence_claim_ids": ["00000000-0000-0000-0000-000000000000"],
  "evidence_source_ids": []
}
```

The response includes a risk label, state, hashes, timestamps, and
`approval.approve_endpoint` / `approval.reject_endpoint` values that Roo can
bind to Slack buttons. Roo owns the visible Slack interaction; the backend
remains the authority for identity, capability checks, state transitions, and
execution.

## Evidence and live preconditions

Evidence identifiers are optional for an explicit user-authored action, but
when present they must resolve to active, currently accessible claims and
sources the acting user may view. Exact source ACL and lifecycle state are
checked at proposal time and again before approval and execution. Revoked,
tombstoned, retired, cross-organization, or newly inaccessible evidence blocks
the transition.

Linear actions require an active, connected, deployment-enabled and
organization-enabled Linear memory configuration. A supplied project must be
one of its selected scopes.

Provider preconditions are read when the proposal is created, when it is
approved, and immediately before execution. The approved snapshot is hashed.
If the live snapshot changes after approval, execution does not run: the
approval is cleared and the proposal becomes `stale` until independently
approved again.

## Execution, failures, and idempotency

Only the adapter registry can execute an allowlisted action. The LLM never
supplies a URL, GraphQL document, HTTP method, credential, or arbitrary
connector operation.

The state machine is:

```text
draft-only: proposed → executing → completed
write: awaiting_approval → approved → executing → completed
review: awaiting_approval|stale → rejected
stale provider state: approved → stale → approved
execution error: executing → failed
reversal: completed → reversing → reversed
```

A completed execution is replayed without another provider mutation. A
provider failure is visible as `failed`, with a bounded error and append-only
event. It is never automatically retried because a network failure can be
ambiguous after a remote mutation. Operators must reconcile the provider and
create a new reviewed proposal when retry safety cannot be proven.

Audit events store action type, risk, actor, request ID, counts, hashes, and
state transitions. They deliberately omit draft bodies, descriptions,
precondition content, result content, connector credentials, and authorization
material.

## Reversal and ingestion

Linear create stores an archive reversal; Linear update stores the exact
pre-mutation field snapshot needed to restore the issue. Before reversal, the
adapter re-reads the issue. If it changed after the MLAI execution, automatic
reversal is refused to avoid overwriting another person's work. Reversal
requires `approve_actions`, `confirm=true`, and its own idempotency key.

After a successful external mutation, the gateway creates one durable
`MemorySourceActionRequest` for the action's Linear configuration. This records
the target external ID and causes the normal provider-artifact/webhook and
organisational-memory synchronization path to ingest the resulting provider
state. Draft-only actions create no ingestion request because they make no
external change.

## Configuration and rollout

Safe defaults:

```text
ORG_MEMORY_ACTIONS_ENABLED=false
ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED=false
ORG_MEMORY_ACTION_REQUIRE_SEPARATE_APPROVER=true
ORG_MEMORY_ACTION_RATE=30/minute
```

Linear execution also requires `linear` in
`ORG_MEMORY_ENABLED_PROVIDERS` and a reviewed `MemoryProviderEnablement` row
for the organization.

Rollout:

1. Apply migration `0020`.
2. Grant `approve_actions` only to named reviewers.
3. Add `org_memory.actions` only to the Admin Roo service principal.
4. Verify draft generation, independent approve/reject, stale-precondition
   invalidation, failure recovery, reversal refusal, and result ingestion in
   staging.
5. Enable `ORG_MEMORY_ACTIONS_ENABLED` while leaving Linear execution off and
   wire the Slack proposal/approval UI.
6. Enable `ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED` for a staged Linear
   organization after connector and scope review.

Rollback is immediate and non-destructive: disable
`ORG_MEMORY_ACTIONS_ENABLED` to stop all gateway access, or disable only
`ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED` to retain local drafts while
blocking Linear provider reads and writes. Existing proposals, results, and
audit history remain available to operators.
