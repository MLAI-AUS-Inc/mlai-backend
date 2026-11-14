# Vibe Security production entry

Implementation candidate; not evidence of production deployment. This reuses the
existing two-phase purchase contract in [cybertest-two-phase.md](cybertest-two-phase.md).

## Explicit configuration

Production entry is off by default. To prepare it, configure the existing
production Django runtime with:

| Setting | Value |
| --- | --- |
| APP_ENV | production (prod alias accepted) |
| DEBUG | false |
| CYBERTEST_ENTRY_PRODUCTION_ENABLED | true |
| CYBERTEST_ENTRY_LOCAL_ENABLED | false |
| CYBERTEST_ENTRY_STAGING_ENABLED | false |
| CYBERTEST_APP_ORIGIN | https://chat.mlai.au |
| CYBERTEST_BASE_PATH | /cybertest |
| CYBERTEST_PRODUCTION_ALL_COMPANIES | true for general access; false preserves pilot mode |
| CYBERTEST_PRODUCTION_COMPANY_IDS | Pilot mode: 1–100 company UUIDs; general mode: may be empty |
| CYBERTEST_BILLING_API_KEY | Independently generated private service credential, 32–512 printable ASCII characters |
| CYBERTEST_BILLING_MODE | disabled for deployment; live only after activation approval |
| CYBERTEST_LIVE_BILLING_ENABLED | false for deployment; true for live purchases |
| CYBERTEST_PHASED_SCANS_ENABLED | true for the phased product before live purchases |

A shared Redis cache is required. Entry also requires an active, verified
CybertestCompanyBinding for the selected company, current MLAI account session,
device ownership, community access and the existing company/organisation policy.
In pilot mode the company allowlist is an additional gate. In general mode, a
first visit after all account/company checks creates one verified binding using
the existing table. Its UUID is stable across visits and unique per company.
Verification here is MLAI company ownership; it does not grant GitHub access.
An existing inactive/pending binding is never reactivated or replaced.
Ownership remains owner-scoped; this does not add team-sharing permissions.
Production rejects test billing and mixed entry flags.

Settings return `entry_mode=production` and `local_test=false` to Chat. Login
uses the existing short-lived PKCE handoff and grant revalidation. GitHub access
is still checked separately by the scanner using MLAI Tools. General-access
identities carry a server-only `workspace_provisioning=true` claim. After GitHub
OAuth and a fresh MLAI grant inspection, the scanner creates the reserved history
workspace if missing. An existing workspace owned by another GitHub actor is
denied, never reassigned. Existing members keep their established access.

## Local verification without IO

Use the repository Python3.11 environment:

```text
python -m unittest roo.test_cybertest_production_config roo.test_cybertest_staging_config
python scripts/test_cybertest_entry.py
python scripts/check_cybertest_production_profile.py
python scripts/check_cybertest_production_profile.py --enabled
python scripts/check_cybertest_production_profile.py --enabled --live
```

These checks use synthetic inputs and block external networking and database
connections. They do not run migrations, charge points or invoke models. Live
mode in the profile check only verifies configuration and mode selection.

## Deployment and rollback

Coordinate the matching Chat and scanner production modes, exact callback,
isolated history/executor allocation and current source/images. Prepare the exact
production0044–0047 migration plan and backup/restore evidence before execution;
local or staging approval is not production execution approval.

During rollout, leave billing and scanner execution disabled until identity,
workspace mapping, routing and storage are verified. Before general activation,
apply scanner history `002-company-workspace.sql` to the isolated production
history database using its separately reviewed migration procedure, deploy the
matching history service and scanner, then deploy this backend. Only then set
`CYBERTEST_PRODUCTION_ALL_COMPANIES=true` and clear the optional pilot list.
Do not use staging credentials, synthetic points or staging storage.

Rollback of general onboarding: set the flag false and restore the pilot list.
Keep all durable bindings/workspaces and the additive history function. This is
an access rollout rollback, not a rollback of reports, receipts or balances.

For rollback, first prevent new scans/quotes and let authorized work settle. Keep
the service credential, production entry, persistent mapping, history and receipt
tables available for status/refunds and saved reports. The existing settlement
endpoints remain authenticated and independent of new-purchase billing mode;
disabling billing is not permission to delete state or reverse migrations.
