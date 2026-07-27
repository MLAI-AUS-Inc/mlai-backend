# Admin Brain read-only pilot rollout

The implementation sequence is complete through the controlled runtime
activation boundary. The next phase is a deliberately small read-only Admin
Roo pilot. This runbook does not itself authorise production access, enable a
feature flag, or modify Public Roo.

`check_org_memory_pilot_readiness` consolidates the existing release gates into
one read-only, content-free JSON report. It does not create users, approve
providers, select scopes, issue credentials, run connectors, enable APIs, or
change feature flags.

The private query, review, and derived-artifact endpoints also require an
active `MemoryPilotDeployment`. A deployment is bound to the exact approved
actors and Slack contexts with organisation-scoped HMACs. Raw actor and
context references remain only in the restricted approval manifest and are
never stored in the deployment row or emitted by deployment commands.

## Human approval manifest

Copy
`plans/org-memory/pilot-approval.template.json` to restricted operational
storage outside source control. Fill it only after the relevant people approve
the values.

The manifest requires:

- one to three exact pilot administrators as `slack:U...` references;
- exact allowed `dm:U...`/`dm:W...` and private `channel:G...` contexts;
- the approved provider list;
- every exact selected source scope as `scope_type:external_id`;
- distinct data, security, review, and operations approvers;
- approval and review-due timestamps, with a maximum review window of one
  year;
- explicit approval of data terms, retention/deletion, backup restoration,
  incident response, freshness/latency/cost SLOs, and Public Roo isolation.

The report emits only the manifest hash and aggregate counts. It never emits
actor, channel, source, query, or evidence identifiers. The real approval
manifest contains operationally sensitive identifiers and should not be
committed.

The checked-in provider governance policy is intentionally still `draft`.
Replace draft owners, exact source selectors, retention periods, cost ceilings,
and provider approvals through the governance process before attempting a
pilot. Do not edit it merely to make the readiness report green.

## Runtime configuration

Set a dedicated secret and an explicit key version in every Admin Brain
runtime that reads or mutates pilot deployment state:

```dotenv
ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION=v1
ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY=<at-least-32-byte-random-secret>
ORG_MEMORY_PILOT_ORGANIZATION_DOMAIN=example.org
```

Do not reuse a service-principal, assertion-signing, Django, connector, or
selector-export secret. Keep the value in the deployment secret manager; only
the version is stored in the database. A missing, short, or rotated key fails
closed immediately. Rotation therefore requires a newly staged approval
binding and an independently reviewed activation; it must not be treated as a
zero-downtime operation.

The operator used to stage, activate, or suspend must have an effective
`manage_sources` capability. A staging or activation operator cannot be one of
the approved pilot actors. The activation operator must also be different from
the staging operator.

`ORG_MEMORY_PILOT_ORGANIZATION_DOMAIN` is used by the deployment-time release
gate only when `ORG_MEMORY_QUERY_API_ENABLED=true`. A flag-on release fails
unless that exact organisation has a current staged or active binding under
the configured HMAC key version and every optional feature remains off. A
flag-off release remains unaffected.

## 1. Preflight

Run against staging first:

```bash
python manage.py check_org_memory_pilot_readiness \
  --organization-domain example.org \
  --approval-manifest /secure/operations/example-pilot-approval.json \
  --governance-manifest /secure/operations/provider-policies.json \
  --environment staging \
  --fail-on-blockers
```

Preflight expects the private query API to remain disabled and reports
`query_api_activation_pending` as a warning. A ready preflight requires:

- a current, exact human approval;
- deployment, organisation, and approval provider lists to align;
- selected database scopes to exactly match the pilot approval and be a subset
  of production governance selectors;
- approved production governance for every enabled provider;
- active connections with reviewed policies, selected scopes, a current
  approved preview, and a completed dry run;
- exactly the approved one-to-three Slack actors with active memberships and
  `view_general_memory`;
- a dedicated active `admin_roo` service principal with `org_memory.read`, a
  usable credential, and no public/action/publication scope;
- publication, actions, Linear execution, selector export, and selector shadow
  flags off;
- a bounded actor-assertion lifetime and clock skew;
- clean work, dead-letter, lease, and outbox queues;
- a completed, alert-free daily reconciliation no older than 36 hours, with
  every active connection healthy;
- source sync times within provider freshness SLOs;
- currently accessible evidence attached to approved active connections;
- passing extraction, consolidation, and retrieval seed suites;
- PostgreSQL plus the installed vector extension for production reports;
- passing Django security checks, including connector credential encryption.

Missing learned-selector labels are a warning, not a blocker for the rules-
based read-only pilot. The 3,000-label selector gate remains independent.

## 2. Stage the exact approved binding

Run the staging command without `--apply` first. It repeats the complete
preflight and rolls back its proposed database change:

```bash
python manage.py stage_org_memory_pilot \
  --organization-domain example.org \
  --approval-manifest /secure/operations/example-pilot-approval.json \
  --governance-manifest /secure/operations/provider-policies.json \
  --operator-email staging-operator@example.org \
  --idempotency-key pilot-2026-07-stage-1 \
  --environment production
```

Review the content-free output, then repeat the exact command with `--apply`.
This creates an immutable staged row containing only the approval hash,
HMAC-pseudonymised allowlist values, aggregate source counts, review expiry,
and operator audit references. It does not enable an API or grant access.

Use a stable, unique idempotency key for each reviewed staging action. Reusing
the same key with different approval data is rejected. A different open staged
binding for the same organisation is also rejected.

## 3. Deploy the live query flag

Only after the staged binding and deployment change are independently
reviewed, deploy `ORG_MEMORY_QUERY_API_ENABLED=true` to the private Admin Roo
backend. Keep every optional publication, action, selector-export, and
selector-shadow flag off.

The query flag alone grants nobody access: the runtime permission rejects every
request until the exact staged binding is separately activated.

The production deployment runs this non-mutating command after migrations and
before restarting the web service:

```bash
python manage.py check_org_memory_pilot_release_gate
```

Operators can repeat it explicitly after activation with the stronger stable-
state requirement:

```bash
python manage.py check_org_memory_pilot_release_gate \
  --organization-domain example.org \
  --require-active
```

## 4. Activate with an independent operator

Run the activation command without `--apply` first:

```bash
python manage.py activate_org_memory_pilot \
  --organization-domain example.org \
  --approval-manifest /secure/operations/example-pilot-approval.json \
  --governance-manifest /secure/operations/provider-policies.json \
  --operator-email activation-operator@example.org \
  --idempotency-key pilot-2026-07-activate-1 \
  --environment production
```

The command runs live readiness while accepting only the exact staged binding
as the activation transition. Review the output, then repeat with `--apply`.
Activation fails if the query flag is not live, the approval has changed or
expired, the HMAC key version differs, readiness has a blocker, or the
activation operator is the staging operator.

After activation, verify the stable content-free state:

```bash
python manage.py report_org_memory_pilot_deployment \
  --organization-domain example.org \
  --fail-if-ineffective

python manage.py check_org_memory_pilot_readiness \
  --organization-domain example.org \
  --approval-manifest /secure/operations/example-pilot-approval.json \
  --governance-manifest /secure/operations/provider-policies.json \
  --environment production \
  --live \
  --fail-on-blockers

python manage.py check_org_memory_pilot_access_matrix \
  --organization-domain example.org \
  --approval-manifest /secure/operations/example-pilot-approval.json
```

The access-matrix gate is non-mutating. It binds the active deployment back to
the exact restricted approval, then evaluates the runtime permission function
without querying memory or Slack. Every approved actor/private-channel pair
and every approved actor-bound DM must pass. Representative unapproved actors,
unapproved private and public channels, and the Public Roo surface must fail.
Its JSON output contains only aggregate expected/pass counts and content-free
blocker codes; it never emits actor or channel identifiers.

After deploying the isolated Admin Roo service, run its credential-bound
signed-request gate:

```bash
python scripts/check_admin_pilot_access.py \
  --env-file .env.admin \
  --approval-manifest /secure/operations/example-pilot-approval.json \
  --organization-domain example.org \
  --slack-team-id T_REPLACE
```

This calls only `GET /api/v1/org-memory/pilot/access-check`. The endpoint
requires the actual service-principal credential, a fresh actor assertion,
verified Slack workspace and identity, current membership/capability, active
pilot actor/context binding, and the live private-query flag. Its successful
response contains only a schema version, `ready`, and a stable code. The Roo
runner checks expected 200 and 401/403 outcomes using aggregate counters and
never sends a query body.

An active row grants access only when the signed Admin Roo assertion contains
an approved Slack actor and an exact approved private channel, or a DM for that
same approved actor. The ordinary membership, capability, service-principal,
source, classification, and ACL checks still apply. Public Roo and public
Slack contexts continue to fail at independent boundaries.

## 5. Evidence collection

During the pilot, collect query traces, explicit relevance/correctness/staleness
feedback, citation and abstention audits, latency, freshness, cost, and access-
denial metrics. Never infer negative relevance from missing feedback.

Freeze the independent audit rubric, fixed pilot window, sample sizes, and
exit thresholds before activation. Use
`docs/org-memory-pilot-evidence.md` for the immutable audit import and
content-free exit report. A green readiness preflight permits only the
approved pilot to start; a green exit report is required before proposing any
scope or user expansion.

## Immediate suspension and rollback

For a suspected leak, deploy `ORG_MEMORY_QUERY_API_ENABLED=false` first because
it is the global stop. Then suspend the organisation binding:

```bash
python manage.py suspend_org_memory_pilot \
  --organization-domain example.org \
  --operator-email emergency-operator@example.org \
  --reason suspected_leak
```

The command is a dry run without `--apply`. Review its count and repeat with
`--apply`. Valid operator-selected reasons include `manual_stop`,
`suspected_leak`, `approval_revoked`, `scope_changed`,
`credential_rotation`, and `pilot_complete`.

At any blocker or suspected leak:

1. set `ORG_MEMORY_QUERY_API_ENABLED=false`;
2. suspend the staged/active organisation binding;
3. revoke the Admin Roo service-principal credential;
4. keep deletion and permission reconciliation workers running;
5. preserve content-minimised audit and deployment records;
6. follow the incident and source-revocation runbooks before considering
   re-enablement.

Suspension is terminal for that deployment row. Re-enablement requires a
current approval, a new staged row, two independent operators, a new activation
action, and all readiness checks. Public Roo remains unchanged throughout this
rollout.
