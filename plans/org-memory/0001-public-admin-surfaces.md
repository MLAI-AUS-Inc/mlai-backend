# ADR 0001: Public Roo, Admin Roo, and published public knowledge

- Status: Proposed; production approval required
- Date: 20 July 2026
- Owners: Security, data, review, and operations owners are recorded in `org_memory/policies/provider_policies.json`

## Context

MLAI already operates Roo as a community-facing Slack bot. The organisational brain will contain committee, executive, finance, email, and other source-restricted evidence. Treating the existing bot, the new administrative assistant, and a future public knowledge product as one security surface would make a prompt or routing mistake sufficient to expose private information.

## Decision

1. The existing Slack app remains **Public Roo** and keeps its currently authorised behaviour.
2. **Admin Roo** is a second Slack app and separately deployed service. It has separate Slack credentials, signing secret, hostname, backend service principal, allowed channels, and operational audit trail.
3. Public Roo never receives an organisational-memory credential. The backend rejects Public Roo at every private-memory endpoint even when a caller supplies a valid administrator Slack ID.
4. The shared Roo codebase will select one explicit, fail-closed surface: `public` or `admin`. Surface-specific skill allowlists are configuration, not model instructions.
5. Admin Roo's first release is read-only and source-cited. It cannot send email, update systems, make payments, or create external commitments.
6. **Published public knowledge** is a separate, optional corpus containing only records explicitly approved for publication. It is not a filtered view over private memory and is not required for Public Roo to continue operating.
7. Admin Roo may retrieve private memory only for an authorised actor in an allowlisted private channel or direct conversation. Personal authorisation does not make a public Slack channel safe.
8. The backend owns identity resolution, capabilities, source ACLs, retrieval filtering, evidence, claims, reviews, and audit. Roo cannot assert a role.

## Required invariants

- Public Roo has no private-memory secret in its environment.
- Public Roo cannot proxy an Admin Roo answer.
- A public-channel request never starts private retrieval.
- Source content is untrusted data and cannot grant access, change instructions, or invoke tools.
- Organisation, actor, channel, classification, provider ACL, revocation, and temporal filters run before ranking or model use.
- Errors do not reveal whether inaccessible evidence exists.
- Admin and Public Roo can be disabled, rotated, deployed, and rolled back independently.

## Consequences

- MLAI operates two Slack apps and two deployments, but can retain one shared Roo codebase.
- Admin pilot installation and channel access can be kept narrow without disrupting the public bot.
- A later public knowledge product needs an explicit publication workflow and physically separate index/table.
- Cross-surface and forged-actor cases are release-blocking evaluation fixtures from PR 0 onward.

## Production approval checklist

- [ ] Name the Admin Roo pilot users/roles.
- [ ] Record allowlisted Slack DM/user and private channel IDs.
- [ ] Name security, data, review, and operations owners.
- [ ] Approve model/data processing terms, retention, deletion, and regions.
- [ ] Create the Admin Roo Slack app in development/test configuration.
- [ ] Approve this ADR and change its status to Accepted.
