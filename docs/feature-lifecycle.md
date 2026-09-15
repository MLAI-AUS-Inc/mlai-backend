# Feature lifecycle register

Reviewed from source on 14 September 2026; next maintainer review is due
14 October 2026. Package ownership below identifies where code belongs. Named
product and operational owners have not yet been assigned, and production usage
has not been measured. No row in this register authorises data deletion.

| Surface | Code owner | Evidence and current disposition | Next decision |
| --- | --- | --- | --- |
| Identity and tenant ownership | core, organizations, founder_tools | Shared JWT moved into core; retained organisations require ownership review before a new link | Define tenant closure, transfer and domain-release policy; approve schema separately |
| Founder reporting and fundraising | startup_updates, vibe_raising | Current feature work includes reporting evidence/revisions in this checkout | Assign reporting owner and retention rules for raw evidence versus approved records |
| Content generation and analytics | content_factory, content_analytics | Current editorial contracts and run callbacks; unused SEO structures already removed | Centralise run transitions; choose one GitHub credential authority; define payload retention |
| External connectors and financial history | integrations | Financial records now reject implicit connection/tenant changes | Model shared upstream accounts explicitly; reconcile existing ownership using an approved data review |
| Community Home, Volunteer and Slack APIs | community_chat, roo | Current API contracts; duplicate Home implementation removed | Identify supported clients and minimum compatibility window |
| MLAI Chat Slack bridge | community_chat, integrations | Live bridge contract; web and dedicated bridge workers were healthy during the 15 September release preflight | Assign operational owner and measure consumer usage before proposing retirement |
| Organisational memory | org_memory | Governed ingestion, retrieval and publication remain first-class | Keep retention and deletion governed by existing subsystem contracts |
| Jobs discovery | jobs | Daily selector queues work; dedicated worker executes it | Add durable recovery with publication idempotency, then review execution retention |
| eSafety | esafety | Preserve the previous decision to keep; earlier audit reports substantial stored data | Confirm current product owner and retention purpose before proposing deletion |
| HealthHack | hospital | Simulation and scoring contracts remain; MedHack game/prediction deletion exists in migration history | Preserve active contracts and scoring-data boundary |
| MedHack compatibility | hospital | Announcement alias and historical team profile fields remain | Measure client dependence before versioning or removing compatibility fields |
| Watt and generic hackathons | hackathons, generic_hackathons | Preserve the previous keep decision; Watt engine shares dependency installation | Review event ownership and archived event retention |
| Studio and Victor AI | mlai_studio, victor_ai | Registered product APIs; presence alone does not establish usage | Assign owners and collect endpoint/consumer evidence |

For each new feature, its change should identify the owning package, product
owner, entry points, workers, data classes, retention decision and review date.
Use “retention undecided” when no policy exists; do not invent a deletion period.
When superseding a feature, record the replacement and compatibility end date.

Retirement needs evidence about API consumers, scheduled work, provider webhooks,
model references and stored data. Separate disabling new entry points from
archiving or deleting historical records. Keep migration history and
compatibility imports until their consumers and fresh-database replay are
accounted for. A table with no recent writes is not automatically safe to drop.

At the next review, assign named owners, resolve the oldest CI omissions,
record actual usage evidence where authorised, and turn each approved retirement
into a bounded change with its own migration proposal.
