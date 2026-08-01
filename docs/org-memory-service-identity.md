# Organisational-memory service identity

Private organisational-memory endpoints do not accept `ROO_API_KEY`,
`INTERNAL_API_KEY`, `MLAI_API_KEY`, or caller-supplied Slack IDs. They require:

1. an organisation-bound service-principal credential;
2. the `org_memory.read` scope;
3. the `admin_roo` surface;
4. a signed, short-lived, single-use actor assertion; and
5. an active, verified Slack identity for the same organisation and workspace;
6. an active organisation membership; and
7. a backend-owned capability grant for the requested memory class.
8. an active `PointsAdmin` record with the exact `committee` class for the
   unified Roo Admin Brain entry points.

Roo never sends a role or capability. The backend resolves both after it has
verified the actor assertion. `PointsAdmin(role="committee")` is an additional
eligibility restriction, never a substitute for verified identity,
membership, capability, private context, and the active pilot manifest.
`UserStartupBinding` remains migration evidence only.

## Configure credential encryption before deployment

Generate a Fernet key outside the repository and put it in the backend secret
store, not source control:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Configure a versioned keyring and active key:

```text
CONNECTOR_CREDENTIAL_KEYS={"2026-07":"<generated-key>"}
CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID=2026-07
```

Production security checks and credential reads/writes fail closed if the
keyring is missing, malformed, or cannot decrypt a value. Existing unversioned
Fernet values can be read for one migration release but are never treated as
plaintext.

After adding a new key while retaining the old key in the keyring:

```bash
python manage.py rotate_connector_credentials --dry-run
python manage.py rotate_connector_credentials
```

Remove the old key only after the real run succeeds and a backup/rollback point
has been recorded.

## Provision Admin Roo identity

1. Apply migrations.
2. In Django admin, add the MLAI Slack workspace team ID to **Organisation Slack Workspaces**.
3. Add each pilot Slack user to **Organisation Identities**. Use provider
   `slack`, the exact Slack team ID as `external_tenant_id`, the exact Slack
   user ID as `external_user_id`, link the Django user, and set `verified_at`.
4. Add an active **Organisation Membership** for that same user and organisation.
5. Create organisation roles, grant only the required capabilities to each
   role, and assign one or more time-bounded roles to the membership. The
   `/auth/context` probe requires `view_general_memory`.
6. Confirm each permitted caller has an active `PointsAdmin` record whose
   exact role is `committee`. `admin`, `partner`, and `portfolio_lead` do not
   inherit Admin Brain access.
7. Issue the internal Admin worker principal:

```bash
python manage.py create_service_principal \
  --name roo-admin-development \
  --organization-domain mlai.au \
  --scope org_memory.read \
  --surface admin_roo
```

The credential is printed exactly once. Store it as `ORG_BRAIN_API_KEY` only in
the internal Admin worker secret store. Public Roo must never receive it.

Issue a distinct route-only principal for Public Roo:

```bash
python manage.py create_service_principal \
  --name roo-public-admin-router \
  --organization-domain mlai.au \
  --scope org_memory.route \
  --surface roo_gateway
```

Store that credential as `ORG_BRAIN_ROUTER_API_KEY` in Public Roo. It can call
only `POST /api/v1/org-memory/routing/eligibility`; it cannot retrieve memory,
traces, feedback, sources, reviews, or actions. Never reuse either principal
for the other purpose.

Rotate with an optional overlap window:

```bash
python manage.py rotate_service_principal \
  --name roo-admin-development \
  --grace-seconds 300
```

Revoke a credential immediately:

```bash
python manage.py revoke_service_principal_credential \
  --credential-id <uuid> \
  --reason "incident or rollback"
```

## Request contract

Admin Roo sends `Authorization: ServicePrincipal <token>` plus a signed
assertion binding the Slack team, acting user, channel, thread, Slack event,
Roo surface, request ID, issued/expiry times, and nonce. The backend verifies
the assertion before resolving the organisation or actor, then records the
nonce durably so it cannot be reused across workers or restarts.

`GET /api/v1/org-memory/auth/context` is a data-free deployment probe. It
returns only the resolved identity context, effective capabilities, and a
memory-class access matrix; it never returns organisational memory.

`POST /api/v1/org-memory/routing/eligibility` is the route-only gateway probe.
It returns only booleans and a policy version. Eligibility is the intersection
of verified actor identity, active membership, capability, exact committee
class, active pilot actor/context, and a DM or private-channel context. It does
not reveal roles, source counts, document metadata, or memory content.

## Capability rules

The seeded capabilities are:

```text
view_general_memory
view_email_memory
view_finance_memory
view_people_sensitive_memory
view_executive_memory
review_claims
manage_sources
publish_knowledge
approve_actions
```

Role assignments and grants have `valid_from`/`valid_until` windows. Multiple
roles may overlap. Active grants accumulate, then any active deny for the same
capability wins. Inactive users, identities, memberships, roles, capabilities,
expired assignments, unresolved identities, unknown classes, and `no_agent`
all fail closed.

The initial memory-class mapping is:

| Memory class | Required capability |
| --- | --- |
| `general`, `internal`, `committee` | `view_general_memory` |
| `email` | `view_email_memory` |
| `finance` | `view_finance_memory` |
| `people_sensitive` | `view_people_sensitive_memory` |
| `executive` | `view_executive_memory` |
| `no_agent` | Never accessible |

Source ACL enforcement is added with the immutable evidence model in PR5; a
classification capability alone will never override a provider ACL.

## Review legacy membership candidates

Generate a report. This command does not create memberships:

```bash
python manage.py backfill_org_memory_memberships \
  --organization-domain mlai.au \
  --output /secure/review/mlai-org-memory-memberships.json
```

Review each candidate and its evidence. Set `approved` to `true` only after
human verification and list exact backend role slugs in `role_slugs`. Then an
active staff reviewer applies that same file:

```bash
python manage.py backfill_org_memory_memberships \
  --organization-domain mlai.au \
  --reviewed-input /secure/review/mlai-org-memory-memberships.json \
  --reviewed-by reviewer@mlai.au
```

The apply step re-derives the candidates and rejects changed evidence,
unresolved identities, inactive users/memberships, unknown roles, a mismatched
organisation, or a non-staff reviewer. `PointsAdmin` contributes supporting
evidence only when another tenant-scoped record already ties that user to the
organisation.

Run the identity report before approval and after identity changes:

```bash
python manage.py report_org_memory_identities --organization-domain mlai.au
```

Resolve every active missing/unverified identity, canonical-versus-legacy user
mismatch, and duplicate email link before enabling Admin Roo.

## Rollback

Revoke or deactivate the Admin Roo service principal first. This immediately
blocks private-memory authentication without affecting legacy Public Roo APIs.
For a single-user incident, deactivate the canonical identity or membership,
or add an explicit deny grant. The identity, membership, and audit records can
remain in place during rollback; do not restore plaintext connector credentials.
