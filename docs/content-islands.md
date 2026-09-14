# Custom content islands

The founder dashboard offers a subject brief, content direction and review flow for any topic, audience need, service, product or feature. Users can choose a suggested direction or write their own.
Directions and example titles are planning starters; search demand is
only represented as measured after research has populated island metrics.

## Creation API

`POST /api/v1/vibe-marketing/islands/custom` (also accepts a trailing slash) uses
the existing authenticated founder/company context. The frontend sends its
company identifier as `companyId`, plus these required string fields:

| Field | Length | Purpose |
| --- | --- | --- |
| `subject` | 1–120 | Any subject, topic or idea to cover |
| `description` | 20–2000 | Scope, goals and boundaries for the content |
| `audience` | 3–300 | Intended readers |
| `focus` | 3–500 | What the content helps readers do |
| `name` | 1–160 | Island display name |
| `keyword` | 1–200 | Audience search theme |

The earlier `productName` field is accepted as an alias for `subject`; conflicting values are rejected. Save identities remain compatible with earlier clients.

The endpoint trims/validates fields and creates a visible `manual` ContentIsland.
It stores the subject, audience and focus together in the existing `description`
column. No model or schema migration is introduced. Creating an island does not
dispatch research or charge Roo Points.

The response is `{ "island": <topic-pillar payload>, "created": true }` with
status 201. Identical normalized briefs in the same organisation have a stable
content-derived slug; retries return the same island, `created: false`, and 200.
Invalid fields return 400 before persistence. Unauthenticated callers and company
access failures follow the existing founder authentication/context contract.

Manual islands are included in the fallback pillar list when automatic islands
are disabled. Island writes participate in the bootstrap cache fingerprint.
Automatic refresh can update measurements and membership, but must preserve a
manual island's name, keyword and brief and must not archive it for missing a
research cycle. Graph nodes expose `researchPending` for manual islands without
stored research; clients display “Not researched” rather than measured zero.

## Topic research

The existing discovery endpoint resolves the island slug within the authenticated
organisation before charging. Stored names, search themes and descriptions take
precedence over client text. Unknown/foreign slugs are rejected; legacy bootstrap
pillars remain supported.

The worker receives `content_island_context` alongside the existing island scope.
It persists that brief with the run and uses it in research context and pillar
strategy. With a brief, research uses the selected search theme and does not seed
from unrelated company keywords, competitors or previously shown topic options.
Existing article/keyword exclusions still apply. Claims remain inputs to
investigate; creating an island grants no editorial or publication approval.

Deploy the worker context support and backend endpoint before enabling the new
frontend. Standard topic-research billing and queue/retry behavior are retained.
