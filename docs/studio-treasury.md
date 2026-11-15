# Studio treasury and calendar contract

The private `/api/integrations/studio-treasury/action` endpoint requires a
dedicated `X-Studio-Treasury-Key` and an enabled deployment. The key must differ
from public Roo, internal and report-source keys. The verified Slack workspace
and requester are supplied by Roo's signed ingress and checked against the
organisation's ledger configuration. Treasury decisions require its treasurer
allowlist. Member meeting confirmations can write only that member's attendance.

Operations are `preview`, `show`, `authorize`, `receipt`, `unknown`,
`meeting_review` and `meeting_time`. Financial operations include a stable
64-hex `request_key`. Previews return a review ID, invoice statuses, original
invoice sources, line-level matching reasons, and requested versus approved
units. Authorisation re-reads all sources, compares their fingerprint, locks the
organisation ledger and reserves every ready invoice as a whole. A pending
invoice is never implicitly split. An existing review authorisation replays its
handoff. Another builder cannot consume its reserved work or pool balance.

The ledger serializes all pools for one organisation and Slack workspace.
Reservations contain stable work and Xero invoice-line identities. Funding
capacity derives from reviewed hours conversions and current issued labour
lines, excluding drafts, voids, credits awaiting line attribution and expenses.
A paid customer invoice retains its original funding identity. A reversed source
with existing consumption causes review. Credits require reviewed attribution
before the remaining line can fund another claim. A cleared bank receipt consumes
the same reservation once. Unknown outcomes keep it reserved.

The initial adapter recognises the same strict external invoice layouts as the
existing Studio source. It validates rate and total independently. A period total
can match only an exact eligible set of tickets. Unrecognised or mixed layouts
remain extraction exceptions for review. A Roo final is matched by its fixed
reference and immutable PDF work streams supplied by the private worker. Form
fields remain editable. Drafts and unreviewed correction rows cannot pass.
Public Roo and the model cannot submit sealed invoice artifacts to the source.

## Operator configuration

After the schema migration is approved and applied, validate a private JSON file
with `manage.py configure_studio_treasury --file PATH`. The default is dry-run.
Use `--apply` to save it and `--verify-opening` only after reviewing its bank
history and customer consumption. Its fields are `organization`, `workspace`,
`configuration`, `opening_evidence` and optional `opening_reservations`.
An update cannot replace an existing ledger's payment history.

`configuration` contains the following source bindings.

| Field | Meaning |
| --- | --- |
| `treasurers`, `members` | Verified Slack IDs permitted to review money or confirm their own time |
| `linear_organization` | Expected Linear organisation UUID |
| `xero_connection_id`, `customer_contacts` | Existing backend-owned Xero connection and eligible contacts |
| `mail_credentials_path`, `invoice_since` | Private treasurer mailbox grant and invoice scan start date |
| `builders` | Linear UUID to `slack_id`, authenticated `senders`, and verified `xero_contact_id` |
| `projects` | Project UUID to `name`, `team_id`, `pool`, `basis` (`size` or `actual`), agreement `version`, `effective_from`, optional `effective_to`, `builder_rate_cents`, optional `calendars` |
| `funding_lines` | Exact Xero `invoice_id`, `line_id`, labour `item_code`, `net_cents`, `currency`, reviewed `hours`, `basis`, `pool`, `start`, `end` |

Each calendar binding supplies `calendar_id`, a private Studio
`credentials_path`, and explicit `title_patterns`. The OAuth grant must include
Calendar read-only scope. Calendar events use recurrence expansion, pagination,
provider occurrence identity and Melbourne dates. Duplicate invitations reuse
one occurrence, independent of Slack threads. Existing and multi-date tickets
are linked when their project and date match unambiguously. Ambiguity stays in
review. Private events and unrelated bookings are omitted. Each person has their
own attendance entry, approval and Linear note. A cancelled or moved invitation
never silently rewrites approved minutes.

Enable `STUDIO_TREASURY_ENABLED` and provision `STUDIO_TREASURY_KEY` only for
the private worker. Enable `STUDIO_MEETINGS_ENABLED` for the public
`/api/integrations/linear/meeting-actions/calendar` endpoint, which uses the
existing Roo API permission and explicit project/member bindings. Customer
finance is inaccessible through this endpoint. The reviewed configuration is
an operator task and cannot be replaced through a conversational model call.

## Regression and rollout

`python -m unittest integrations.test_studio_payment_rules` runs without a
database. The October regression requests 101 hours from an 80-hour customer
line and reports 21 hours without cover even though the money would fit. Test
database migration approval is required before running the database suites.
Pilot a client with a reconciled opening ledger in preview, inspect source
exceptions, then enable Roo handoffs. Production deployment and migrations use
the repository's separate approval and release process.
