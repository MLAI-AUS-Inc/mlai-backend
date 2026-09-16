# Content islands

The founder dashboard has two actions beside Map/List: **Create an island** for
exploring a new subject, and **Find more islands** for normal company discovery.
The duplicate creation card, Learn more button and summary chips are removed.

## Research a new subject

The builder asks for a few words or a topic description, optional audience and
boundaries, then search intent (all, informational, commercial, transactional,
navigational or a custom direction). Users do not choose an island name or a
final target keyword. A review step explicitly confirms a research run costing
one Roo Point using the existing content-island research price and its configured
free-domain exceptions. Adding a researched result is free. Article idea
generation remains a separate paid action.

`POST /api/v1/vibe-marketing/islands/research` (also accepts a trailing slash)
uses authenticated founder/company context and accepts:

| Field | Limit | Purpose |
| --- | --- | --- |
| `companyId` | Existing company identifier | Authorised company scope |
| `subject` | 1–1000 characters | Topic words or a description |
| `description` | 0–2000 characters | Optional context and boundaries |
| `audience` | 0–300 characters | Optional readers |
| `searchIntent` | `any`, `informational`, `commercial`, `transactional`, `navigational`, `custom` | Preferred search intent |
| `focus` | 3–500 when intent is `custom` | User-defined purpose; ignored for other intents |
| `clientRequestId` | 8–100 characters | Stable identifier for this research attempt |

The server derives a bounded dispatch/billing key from the company, payer,
normalised brief and client identifier. Invalid input is rejected before charging.
Retries return the existing run without charging or dispatching it again; a lost
queue response is reconciled through the existing worker dispatch-key lookup.
Uncertain dispatches are polled until their outcome is known, never immediately
refunded while a possible worker job continues. Insufficient balance returns 402.

The response contains `runId` and `status`. The existing authenticated run-status
endpoint returns progress and, once complete, `result.suggested_islands` with up
to five measured proposals. The frontend stores the brief and run reference in
company-scoped session storage, so closing/reopening the dialog or refreshing the
page resumes the same run. Storage failure only disables reload recovery.

### Worker pipeline

The backend dispatches `/api/runs/island-research`, a paid, service-authenticated
variant of the `island_refresh` workflow. Scheduled `/api/runs/island-refresh`
remains free and scheduler-only. No database schema changes are required.

1. Explore up to eight short initial seeds: the subject, synonyms, expanded
   abbreviations and direct applications. Always perform a follow-up search;
   sparse results get a third pass, with up to six new seeds per pass.
2. Query DataForSEO suggestions (up to 500 per seed), related searches (depth 3,
   up to 300 per seed), and semantic keyword ideas (500 per group of four seeds).
   Keep Australia/English consistent. Calls have bounded concurrency and transient
   provider failures are retried; errors are never interpreted as no demand.
3. Deduplicate positive-volume candidates and check all of them for strict topical
   relevance before ranking. Audience need not appear literally in the query.
   Search intent prioritises compatible themes rather than rejecting every query
   whose provider label differs. Explicit exclusions still apply. Hydrate missing
   difficulty for all relevant candidates in provider-sized batches.
4. Run the normal embeddings, clustering, naming and opportunity scoring on up to
   1,000 relevant measured keywords, preserving declined-topic exclusions. When
   fewer than five normal clusters form, recover smaller coherent groups and
   individual measured starting points. Options with fewer than three queries
   carry `limited_data: true`; the UI explains their narrower evidence.
5. Return up to five proposals: established clusters first, then intent-compatible
   options and opportunity score. Each includes actual monthly search estimates,
   difficulty and underlying keywords. Adoption accepts even a single measured
   keyword but rejects empty, zero-demand or missing-difficulty evidence.

The LLM only derives seeds, checks relevance and names measured clusters. Search
volume and difficulty always come from DataForSEO; no-data responses never turn
seed guesses into islands. The displayed volume is the sum of keyword estimates,
not a count of unique readers. Unchosen proposals remain in the run result and do
not enter the organisation's keyword pool or affect scheduled island lifecycle.

Failed or empty research refunds the recorded payer's actual charge once. The
worker's normal run-sync callback performs this even after the browser closes;
status polling also reconciles it. The existing ledger refund idempotency key is
used. A new deliberate attempt after a terminal result uses a new request ID.

### Select several measured themes

The results screen has individual checkboxes, Select all/Clear selection, and a
review step before the free batch save. Selection survives closing the dialog or
refreshing the page. Already-added themes are labelled and cannot be duplicated.
The success screen can return to remaining results without another research fee.

`POST /api/v1/vibe-marketing/islands/research/<runId>/adopt` accepts `companyId`
and `proposalIds` (1–5 stored proposal identifiers). With `preview: true` it
returns `groups` (names, proposal IDs and deduplicated metrics) and `already_added`
without writing. Without preview it atomically saves and returns `islands`.
The legacy singular `proposalId` request remains supported.

Only stored, completed, company-owned research is authoritative. Compatible
search intents and complete-link centroid similarity of at least 0.90 group
closely related selections; a chain of loosely related themes cannot bridge
otherwise distant islands. Keyword metrics are deduplicated. Existing exact
islands are reused. Requests and concurrent retries serialize on the organisation
and research run, recording adopted IDs in `result.island_research_selection`.
No extra payment or research dispatch occurs during review or adoption.

### Daily growth, merging and splitting

Founder-selected measured islands join the existing daily refresh and rotating
DataForSEO expansion. `GET /api/seo/islands/` includes service-only
`dynamic_scopes`: the saved brief, selected seed centroids, existing memberships,
and a revision. Written keyword associations remain available to evolution.
These members bypass the normal global opportunity-ranked keyword cap, so a
smaller selected theme cannot disappear just because other keywords rank higher.

The worker assigns new keywords to the closest selected scope with similarity
at least 0.80. Separate research briefs remain boundaries in this first version.
It clusters that scope's measured keywords, preserves sparse selected themes,
and proposes merges using stricter complete-link centroid similarity of 0.90.
Declined topics remain excluded. The normal unselected company pool follows its
existing lifecycle independently.

Bulk sync applies those partitions under the same organisation lock as adoption.
Membership/metric growth keeps island identities. A structural merge or split
needs the same topology on two distinct increasing research dates. Duplicate
callbacks, old dates and stale selection revisions cannot confirm or overwrite
newer choices. Missing evidence does not retire an island. The largest membership
overlap keeps its stable slug; additional split branches receive durable slugs.
Merged records and their snapshots are archived, never deleted; their retained
memberships preserve article history. Old island discovery links resolve to the
current survivor. The run records redirects and the last 100 topology changes.
The compact browser run exposes only adopted proposal IDs, not this internal
state. Worker callbacks preserve this Django-owned state.

This uses the existing run JSON, island membership and snapshot models; no schema
migration is required. Existing manual islands do not change behavior unless a
founder selects their measured theme through the batch flow. A theme already
managed by another research brief is reused without transferring ownership.

## Existing manual islands and older clients

The legacy `POST /api/v1/vibe-marketing/islands/custom` endpoint remains compatible
for older clients. It accepts `subject` (or the earlier `productName` alias),
`description`, `audience`, `focus`, `name` and `keyword`, validates them, then saves
a free manual island with a content-addressed slug. It does not trigger research.
The current builder uses the paid research-and-adopt path above.

Existing manual islands remain visible in the bootstrap when automatic islands
are disabled. Automatic sync preserves their name, keyword and brief, and does
not archive them for missing a cycle. Unresearched manual islands carry
`researchPending`, displayed as “Not researched” rather than measured zero.

The article discovery endpoint resolves each island within the authenticated
organisation before charging, using stored context rather than client overrides.
Its worker uses the island's keyword and brief, excluding unrelated seeds and
competitors. Existing written/declined-topic exclusions still apply. Research
never grants editorial or publication approval.

Deploy the worker, then the backend, then the frontend. The worker endpoint must
be available before the frontend offers the research confirmation.
