# E5 / v2 3.1: recommend a publishing channel from current site facts

The instruction to continue all implementation phases authorizes this design and
code. Keep rollout disabled; this step publishes no content, creates no account,
stores no credential and changes no repository or connection authority.

## Inputs and result

Read the owner's already selected company domain, an optional E1 fingerprint at
its exact repository/source/branch/root, and a bounded public homepage observation.
Reuse the existing HTTPS address validation and pinned connection for a separate
homepage observer. Read at most a 64 KiB prefix plus one truncation byte and
follow at most two canonical apex/www homepage redirects. This observer is
informational and cannot supply the full-route live verification proof.
Never follow URLs advertised by page markup or forward cookies/credentials.

Parse structural generator metadata, official API-discovery relations and a small
allowlist of platform-specific resource hosts/attributes. Ignore prose, comments,
script text and arbitrary links. Keep only fixed signal IDs, confidence, a body
digest and observation time; do not persist HTML or header values. Page claims are
informational and can be missing or spoofed. Conflicting strong claims remain
unknown rather than selecting a connector arbitrarily.

A scoped confident repository fingerprint with a known framework recommends
`git`. Otherwise a recognized CMS recommends `cms`, carrying its platform name;
an unknown or unavailable site recommends `hosted`. Always include `hosted` and
`portable` alternatives. This is a routing recommendation, not a supported-
framework certificate or an authenticated publishing capability. Existing root,
branch, source, owner-consent and provider verification gates remain required.

The closed versioned record belongs under
`OrganizationContentConfig.article_system.publishing_channel`. It distinguishes
the backend recommendation from an explicit owner selection. Worker/repository
input cannot replace the owner's selection. Observation and selection are fenced
to the selected company/site and a revision. The 3.2 channel interface must use a
current backend-owned selection/reservation; this JSON is never proof of access.
No database migration is required.

## Delivery boundary

Implement the bounded parser/classifier and saved public-page fixtures first.
Then wire a default-off owner inspection/selection action through the existing
company-scoped website surface, preserving channel state through normal scan
merges. Public inspection needs no GitHub connection. Reads of status only project
saved results. Remote observation happens outside organization locks, followed by
scope/revision rechecks before persistence. Stale, disconnected or superseded
inspection cannot overwrite a later owner choice.

Implemented behind `ARTICLES_CHANNEL_DISCOVERY_ENABLED=false`: existing owner
status plus `inspect-channel` / `select-channel` actions, connect-time inspection
after the bind transaction, and local E1 refresh after a current authenticated
scan. Normalization discards incoming worker channel data; normal scan merges and
the serialized config PUT retain the current backend-owned record. Source-only
configuration changes retain the owner's preference. Generation, site, repository,
branch/root and connection-state changes invalidate its active projection.

The first channel implementation stays with the existing Git flow and adapters;
CMS credentials/publishing and hosted-domain provisioning belong to 3.2–3.4.
Unknown sites must retain the hosted/portable path even while a public observation
fails. Do not enable this surface until the selected channel handlers are ready.

## Sources and evidence

WordPress documents the `https://api.w.org/` discovery relation in both an HTTP
Link header and an HTML link element, including installations using `rest_route`
instead of pretty permalinks. Detection records the signal without following its
advertised endpoint. [WordPress discovery](https://developer.wordpress.org/rest-api/using-the-rest-api/discovery/)

Ghost documents its head/footer helper and supplies a public demonstration site
for representative theme output. [Ghost helper](https://docs.ghost.org/themes/helpers/utility/ghost_head_foot),
[Ghost demo](https://demo.ghost.io/). Saved fixtures must distinguish observed
markup from deliberately synthetic parser/host-confusion attacks.

The saved `content_factory/fixtures/publishing_channels/public-platforms-v1.json`
contains structural excerpts observed on 17 October from the WordPress developer
site, Ghost demo, Webflow, Wix, Squarespace and Shopify's Dawn demo linked from its
[official theme listing](https://themes.shopify.com/themes/dawn/presets/dawn).
Each records the observed response-prefix digest, time and coverage. The first
64 KiB of the WordPress page has no generator/discovery tag; its actual HTTP Link
header supplies the documented relation. Fixtures replace identifiers/resource
paths and omit all scripts/prose. They are minimized observations, not complete
original response bodies or proof of an authenticated connector.

Local evidence: 18 database-free tests cover all six observed platforms,
unsupported/manual/foreign-source adapters, malformed/inert/spoofed markup,
conflicting generators, mixed private DNS, credential/cross-origin/path redirects,
byte bounds, failed observations, existing owner HTTP/company scoping, disabled
behavior, concurrent choice/site/source/configuration changes and preference
preservation. The full website CI group and failure contract audit are also run.
One actual persistence/consent-locking regression is added to the existing remote
database-backed `WebsiteReviewRegressionTests`; it is not run locally.

Acceptance requires each listed CMS platform, a scoped known-framework Git site,
unknown/unreachable sites, conflicting/spoofed signals, malformed/oversized HTML,
credential-bearing URLs, stale scope/revision, owner-choice preservation and
proof that detection never grants readiness or executes page code. Actual channel
publication remains separate acceptance evidence.
