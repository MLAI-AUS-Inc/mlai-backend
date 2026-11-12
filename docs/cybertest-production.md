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
| CYBERTEST_PRODUCTION_COMPANY_IDS | Explicit comma-separated company UUIDs, 1–100 entries |
| CYBERTEST_BILLING_API_KEY | Independently generated private service credential, 32–512 printable ASCII characters |
| CYBERTEST_BILLING_MODE | disabled for deployment; live only after activation approval |
| CYBERTEST_LIVE_BILLING_ENABLED | false for deployment; true for live purchases |
| CYBERTEST_PHASED_SCANS_ENABLED | true for the phased product before live purchases |

A shared Redis cache is required. Entry also requires an active, verified
CybertestCompanyBinding for the selected company, current MLAI account session,
device ownership, community access and the existing company/organisation policy.
The company allowlist is an additional gate; it does not grant ownership or create
a workspace. Existing ownership remains owner-scoped; this change does not add
team-sharing permissions. Production rejects test billing and mixed entry flags.

Settings return `entry_mode=production` and `local_test=false` to Chat. Login
uses the existing short-lived PKCE handoff and grant revalidation. GitHub access
is still checked separately by the scanner using MLAI Tools.

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
workspace mapping, routing and storage are verified. Activate only explicit
company IDs. Do not use staging credentials, synthetic points or staging storage.

For rollback, first prevent new scans/quotes and let authorized work settle. Keep
the service credential, production entry, persistent mapping, history and receipt
tables available for status/refunds and saved reports. The existing settlement
endpoints remain authenticated and independent of new-purchase billing mode;
disabling billing is not permission to delete state or reverse migrations.
