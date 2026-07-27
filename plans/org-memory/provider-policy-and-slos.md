# Organisational-memory provider policy and SLO contract

The machine-readable source of truth is `org_memory/policies/provider_policies.json`. Its production default is deny. A provider cannot pass the deployment gate until its scope, authority, retention, owners, terms review, SLOs, and cost ceilings are approved.

## Initial source matrix

| Provider | Initial scope | Default classification | Authority | Explicit exclusions | Status |
|---|---|---|---|---|---|
| Google Drive | Selected transcript folders and Shared Drives | Committee | Meeting testimony; explicit decisions or corroboration required | Personal drives, unselected folders, recordings without transcript consent | Draft/disabled |
| Slack | Selected shared committee/project channels | Committee | Informal discussion; low unless corroborated/reviewed | All DMs, unselected channels, ephemeral content | Draft/disabled |
| Linear | Selected teams/projects | Committee | System of record for issue/project structured fields | Private and unselected projects | Draft/disabled |
| Notion | Selected root pages/data sources | Committee | Published documentation; page/status-specific authority | Private, unselected, and archived pages | Draft/disabled |
| Gmail | Approved accounts plus explicit labels | Executive | Correspondence evidence; material commitments require review | Personal accounts, unlabelled mail, auth/medical/HR/legal mail | Draft/disabled |
| Stripe | Approved aggregates only | Finance | System of record for approved revenue/subscription aggregates | Payment credentials, raw payment details, customer PII | Draft/disabled |
| Xero | Approved aggregates only | Finance | System of record for approved accounting aggregates | Raw bank transactions, bank details, contact PII | Draft/disabled |
| Luma | Selected calendars/events and approved aggregates | Internal | System of record for selected event dates/aggregates | Attendee PII and private registration answers | Draft/disabled |

## Ownership gate

The following named people must be recorded before any production provider is enabled:

| Role | Responsibility | Owner |
|---|---|---|
| Data owner | Approves source purpose, authority, scope, and deletion | TBD |
| Security owner | Approves credentials, access controls, incident response, and provider terms | TBD |
| Review owner | Owns contradictions, sensitive claims, corrections, and escalations | TBD |
| Operations owner | Owns connector health, freshness, budgets, and dead work | TBD |
| Privacy/legal owner | Approves retention, consent, people data, email, and recording use | TBD |

## Proposed launch SLOs

These are initial engineering targets and remain draft until an owner approves them in the policy manifest.

| Measure | Initial target |
|---|---|
| Verified webhook/change notification to answerable state | 10 minutes p95 |
| Slack thread after quiet period | 15 minutes p95 |
| Polled source change | 24 hours p99 |
| Notified deletion/access revocation excluded from new answers | 15 minutes p95 |
| Dead work or expired credential operator alert | 60 minutes |
| Daily health report | By 08:00 Australia/Sydney |
| High-risk review | 8 business hours |
| Standard review | 3 business days |
| Oldest review-item alert | 5 business days |

The daily and monthly model budget values and escalation destination remain deliberately unset. Model-backed work pauses and alerts when an approved limit is absent or reached; deterministic permission/deletion reconciliation continues.

## Decisions still requiring MLAI approval

- Exact Admin Roo pilot members and allowed private Slack contexts.
- Drive folder/Shared Drive IDs, transcript cutoff, supported recording/transcription consent, and expected volume.
- Slack internal-app/export acquisition method, selected channel IDs, retention, and terms-review owner.
- Notion roots; Linear teams/projects; Gmail accounts/labels; Stripe/Xero approved aggregates; Luma calendars/events.
- Raw evidence, derived memory, query log, and backup retention/deletion periods.
- People-memory consent, allowed facts, prohibited inferences, and reviewer.
- Model provider, data retention/no-training terms, processing region, and incident path.
- Daily/monthly spend limits, review escalation destination, and SLO approver.

## Enforcement

Run:

```bash
python manage.py validate_org_memory_governance --environment production
```

`ORG_MEMORY_ENABLED_PROVIDERS` is a comma- or space-separated list. In production, any requested provider fails validation unless its policy is enabled and fully approved. Future connector entry points must also call `assert_provider_ingestion_allowed(...)` before fetching content.

The Drive metadata inventory uses a narrower, separate approval. Add the exact `organization:<id>`, `connection:<id>`, and selected roots as `folder:<drive-id>`; approve the provider's `inventory` record, name data/security approvers, and set `inventory.max_files`. This permits metadata inspection only and leaves `production_enabled` false.
