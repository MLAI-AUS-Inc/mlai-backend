# Admin Roo staging pilot handoff

## Current state

The code path is ready through the manual staging release boundary. No Admin
Roo pilot has been staged or activated, no feature flag has been enabled, and
Public Roo has not been changed by this operational step.

The local operational preflight could not proceed against the configured
server database because the database hostname was not resolvable from this
workspace. The local environment also has no pilot HMAC secret, pilot
organisation setting, or completed approval manifest. The checked-in approval
and provider-governance files remain intentionally draft.

## Inputs that require explicit owners

Before the first flag-off backend deployment and staging action:

1. Data, security, review, and operations owners must approve the exact pilot
   manifest.
2. Governance owners must approve the exact providers, source selectors,
   retention periods, SLOs, cost ceilings, incident controls, and Public Roo
   isolation.
3. Slack administrators must create the separate Admin Roo app and provide its
   dedicated bot token, signing secret, private `G...` channel IDs, and/or
   approved DM actor IDs.
4. Backend operators must configure a dedicated HMAC secret/version and
   `ORG_MEMORY_PILOT_ORGANIZATION_DOMAIN`.
5. Two non-pilot operators with `manage_sources` must be nominated for staging
   and activation.
6. A dedicated `org_memory.read` service principal must be issued once and
   stored only in Admin Roo's mode-0600 environment file.
7. Network access to the deployment PostgreSQL database and the protected
   `admin-roo-staging` GitHub environment/secrets must be available.

## Exact safe sequence

1. Deploy backend PR25/PR26/PR27/PR28 code with
   `ORG_MEMORY_QUERY_API_ENABLED=false`.
2. Run the complete readiness preflight.
3. Dry-run and apply `stage_org_memory_pilot` with the first operator.
4. Deploy `ORG_MEMORY_QUERY_API_ENABLED=true`; the release gate must see the
   exact current staged binding.
5. Dry-run and apply `activate_org_memory_pilot` with the second operator.
6. Require active backend release/deployment reports and run
   `check_org_memory_pilot_access_matrix` against the same restricted approval
   manifest.
7. Validate `.env.admin` against the same approval manifest.
8. Manually run the protected Admin Roo staging workflow; it must pass the
   aggregate credential-bound signed-request gate.
9. Exercise one real approved Slack mention/DM and denial path without logging
   query content.
10. Start the fixed audit window; suspend immediately on any blocker or
    suspected leak.

Production or scope expansion remains evidence-gated by the immutable exit
policy and independent audit process.
