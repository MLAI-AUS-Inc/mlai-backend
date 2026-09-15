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

1. Derive at most four short search seeds from the topic brief.
2. Fetch DataForSEO Labs keyword suggestions and related keywords (Australia,
   English, matching current island research defaults), then hydrate missing
   difficulty from its bulk difficulty endpoint.
3. Deduplicate and retain positive measured volume and measured difficulty.
   Apply provider intent when present and a bounded semantic relevance check
   against the brief, audience and intent. There is no fallback to company
   competitors, unrelated saved topics or synthetic metrics.
4. Run the existing island embeddings, clustering, naming and opportunity
   scoring on this scoped pool. Apply declined-keyword feedback and require at
   least three measured keywords per proposed island.
5. Return up to five proposals ranked by the existing opportunity score, with
   monthly search estimates, average difficulty and underlying keywords.

The LLM only derives seeds, checks relevance and names measured clusters. Search
volume and difficulty always come from DataForSEO; no-data responses never turn
seed guesses into islands. The displayed volume is the sum of keyword estimates,
not a count of unique readers. Unchosen proposals remain in the run result and do
not enter the organisation's keyword pool or affect scheduled island lifecycle.

Failed or empty research refunds the recorded payer's actual charge once. The
worker's normal run-sync callback performs this even after the browser closes;
status polling also reconciles it. The existing ledger refund idempotency key is
used. A new deliberate attempt after a terminal result uses a new request ID.

### Add a measured result

`POST /api/v1/vibe-marketing/islands/research/<runId>/adopt` accepts `companyId`
and `proposalId`. The server verifies the run belongs to the authorised company
and is complete, and loads the proposal from its stored result. Client-supplied
names, metrics or keywords have no authority. No payment occurs here.

An exact existing theme is reused. New themes use a deterministic keyword-based
slug and the existing manual-island origin so they remain on the founder's map.
Their measured keywords, centroid, metrics and snapshot are persisted atomically;
related edges are rebuilt. The original brief and intent guide subsequent article
idea research. No unrelated islands are renamed, missed, archived or removed.
Retries return the same island. Legacy manual islands without research can acquire
the first measurements through this path.

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
