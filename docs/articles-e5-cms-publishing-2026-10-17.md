# E5 / v2 3.3: CMS authority and WordPress, then Ghost

This implements the owner's instruction to continue all phases. All code remains
behind default-off flags and draft PRs. No real CMS key, account, website write,
wallet action, deployment or backend migration is included.

## Implemented connection boundary

`ARTICLES_CMS_CHANNEL_ENABLED=false` requires the separate channel-execution and
channel-discovery flags. The existing authenticated company action `cms-connect`
accepts the saved channel scope/revision, a fresh UUID `client_request_id`,
`platform`, `public_origin`, `api_origin`, `credential_origin_confirmed=true`,
and the application/integration credential (`username` for WordPress, `secret`).
It requires the owner's saved CMS choice. It refreshes founder/company/site
before HTTP and repeats the choice, owner and selection after HTTP under the
organization lock. A replay reuses the acknowledged binding; different settings
on the same request ID conflict, and a revoked or superseded request cannot
recreate a connection.

The WordPress root index must report the exact selected `home` and installation
`url`, followed by authenticated `users/me?context=edit` with `edit_posts`.
Ghost's unauthenticated `site/` must name the selected public site, followed by
an authenticated bounded posts request. Neither public response alone proves
authentication. An owner explicitly confirms a separate Ghost admin origin;
it is never inferred from a homepage link. Names, email, HTML and raw provider
responses are discarded. This is provider identity, not a public live proof.

A repository-to-CMS switch reserves the existing composite keep-content
disconnect, including its cancellation/refund outbox. The new binding uses no
GitHub identity and clears repository-derived projections. Channel preference is
rebound to the new saved scope. Private credentials are excluded from operation
and status projections. Every lifecycle generation change erases private key
payloads and retains a CMS discriminator tombstone so even Git cleanup/token
paths reject an old CMS connection. CMS disconnect remains available with the
rollouts disabled and tells the owner to revoke the application/integration key
in the provider too. The backend cannot revoke an arbitrary provider-owned key.

Public-post capture now uses the common `capture_existing` operation and owner
`capture-existing` action with the current connection tuple plus a fresh UUID
`client_request_id`. One `cms-capture` reservation advances a bounded page per
short lease. Every provider read reloads the current selected founder/company,
credential reference and generation; results are checked again before saving.
Expired leases can be replaced; a late reply cannot overwrite a newer worker or
cancellation. The generic reconciler resumes these jobs only with CMS execution
enabled and never treats a frozen CMS job as Git cleanup.

WordPress posts are ordered by ID with authenticated edit context, published
status and fixed page size. Ghost posts use fixed public/published filtering,
HTML format and bounded pagination. Original HTML stays private in the capture
manifest with content digests and native author/category/tag/media IDs; it is
never executed. Password-protected/member-only bodies and author emails are
excluded. Public receipts contain counts, coverage and an inventory digest only.
Changing pagination can restart the same capture twice. Transient page reads
retry with bounded backoff; exhausted or invalid capture records shared B1
failure evidence for the goal's later fallback handling. Page, record and byte
limits remain explicitly partial, and purge erases captured private manifests.
No CMS source is represented as a Git commit or on-main publication fact.

**Still incomplete:** media, durable post publication/reconciliation,
unpublish, live proof, CMS goal milestones, artifact-only CF envelope, owner UI,
and the remote WordPress/Ghost acceptance runs. Connection summaries deliberately
advertise no generation or publication readiness. Keep rollout flags off.

Local evidence: the full 925-test website group includes 66 CMS credential,
transport, identity, owner and capture cases; 15 observability and 24 Chat facade
tests also pass. Two actual ORM
credential/connect/replay/disconnect regressions are assigned to disposable CI
and have not been run locally. The final run and exact PR CI head are recorded
in the PR validation notes.

Capture validation adds pure pagination/manifest and actual-guard lease/race
cases; its actual ORM capture/purge path is assigned to the remote disposable
connection regression. Full WordPress/Ghost container and goal acceptance remain
required; a complete inventory is an observation, not a live publication proof.

## Independent provider authority

Use an explicitly authorized non-repository WebsiteConnection, with empty GitHub
repository/installation/branch/root and null repository ID. A closed backend-owned
channel discriminator identifies CMS + platform + company + public site + founder.
It is stored only by the owner connection controller. Git/token/read/build guards
must reject this discriminator before any GitHub call. CMS uses its own guard;
page detection and the channel preference remain informational.

Keep the active credential in a private `channel-credential` operation payload,
using the existing versioned `encrypt_credential_value` / `decrypt_credential_value`
keyring helpers. Never reuse GitHub columns or pass a credential to Content Factory,
GitHub, a browser preview, article JSON, logs or public receipts. The private
operation action is excluded from owner operation reads. Connection summaries
contain only an opaque provider reference and bounded capability status. Existing
JSON columns and lifecycle records avoid a new database migration.

Connect verifies the exact founder/company/domain and the current channel-choice
revision, reads the provider identity outside locks, then repeats all checks before
persisting a new generation/binding and encrypted credential. No guessed API origin
receives a token. CMS API URLs must be HTTPS on the owner's explicitly selected
origin, without userinfo or secret query data; DNS/socket/TLS are pinned and every
credential-bearing redirect is refused. Ghost can use a different admin domain;
that domain requires a separately proved owner binding before credential use.

## WordPress operations

Use an Application Password with HTTPS Basic authentication. Read the authenticated
current user and permissions, capture posts page by page, and keep inventory
completion/coverage explicit. Fixed bounded query parameters and response limits
prevent unbounded provider output. API error bodies are not returned or logged.
Posts map title, HTML, slug, status, author, categories, tags and featured-media
references. Only explicitly mapped fields are written. Media uploads use already
owned artifact bytes; arbitrary image URLs are not fetched with credentials.
Draft versus publication follows the saved original owner review/auto policy.

A durable operation records the provider binding/generation, owner request,
article/approval digest, deterministic operation marker and attempted external
action before HTTP. A timeout keeps that same operation and reconciles the marker
and content using authenticated reads. It never blindly repeats POST or edits an
unrelated pre-existing slug. Store the returned post/media ID before later effects;
repeating an acknowledged operation reuses those IDs. Unknown outcomes remain
bounded pending work rather than an invented success.

The post recovery contract reserves an operation-unique public slug (a bounded
title seed plus the full operation UUID), sealed along with the original artifact,
editorial and approval digests before dispatch. A fixed GET checks that slug first.
The sole POST sends the approved body and requested draft/published status
together; it does not promote or overwrite an unrelated CMS draft. Persist
`create_issued` before sending. A lost reply can only probe that same slug and
validate the exact operation marker, body, title, status and native references.
After eight empty probes the outcome remains explicitly unknown: no new POST,
second public copy or fabricated live proof follows. A recovery read is not
authority to republish through another channel while the first publication's
outcome is unknown. Provider acknowledgement remains separate from public proof.

WordPress's documented update handler has no optimistic body/version check like
Ghost's `updated_at` contract. The initial create path therefore sends the approved
content and publication status in one new-post request. It does not claim atomic
compare-and-swap for later WordPress edits. Existing customer posts remain intact;
edit/unpublish flows need their own explicit owned-resource review and checks.

Verify the public post URL and expected content/operation marker independently of
the authenticated API response. A draft/export or provider 201 is not live proof.
Provider URLs cannot redirect verification to a private or foreign origin.
Unpublish requires explicit owner intent and the exact current MLAI-owned post
reference/content; disconnect erases stored credential material and revokes future
operations while retaining published content by default. Tell the owner to revoke
the corresponding WordPress Application Password as well.

## Ghost operations

Use the owner-provided Admin API integration key only on the backend. Generate a
fresh HS256 JWT per request with the key ID, `/admin/` audience and expiry at most
five minutes. Use a pinned API-version header and bounded paginated JSON. The
posts/images adapters share the same reservation, ownership, uncertain-outcome,
review, verification and disconnect contracts. Do not reuse Content API keys or
browser sessions for write authority. Confirm current posts/image/HTML conversion
semantics against primary documentation before implementation.

## Pipeline and evidence

A CMS generation is artifact-only in Content Factory. Provider credentials and
publishing stay on the backend. Preserve editorial policy, reserved billing,
mandatory review, original draft identity and body digest. Add channel-specific
proof kinds/waivers for repository-only milestones; never fabricate a commit,
GitHub review, merge or CI attestation. The shared status and publishing interface
must be used throughout the CMS goal. Original Git goals and portable exports keep
their current semantics.

Before rollout, require fake-transport security/race/idempotency tests, then a
remote disposable WordPress container exercising connect, paginated capture,
draft/publish/media, exact public verification, uncertain replies and disconnect.
Add Ghost under the same contract. Production transport always retains public
HTTPS pinning; any container-only transport injection lives in tests. Run no local
database, migration, Docker or framework compiler. E4's dedicated account/wallet,
distinct hostnames and missing environment/token remain separate live-zoo blockers.

## Primary references checked on 17 October 2026

- [WordPress authentication](https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/)
- [Application Passwords](https://developer.wordpress.org/advanced-administration/security/application-passwords/)
- [Posts](https://developer.wordpress.org/rest-api/reference/posts/), [media](https://developer.wordpress.org/rest-api/reference/media/), [users](https://developer.wordpress.org/rest-api/reference/users/)
- [Ghost Admin API](https://docs.ghost.org/admin-api/)
- [WordPress root site identity](https://developer.wordpress.org/reference/classes/wp_rest_server/get_index/)
- [Ghost site identity](https://docs.ghost.org/admin-api/site/overview/), [HTML creation](https://docs.ghost.org/admin-api/posts/creating-a-post/), [update collision detection](https://docs.ghost.org/admin-api/posts/updating-a-post/)


## Bounded image transport (implementation continuation)

The media transport accepts only verified, decodable, static PNG/JPEG/WebP bytes, at most 10 MiB and 4096 pixels per dimension (16 megapixels). The fixed ASCII filename carries an opaque upload operation and source digest; caller text cannot become a header. Source bytes and returned storage URLs are absent from object representations.

WordPress uses a fixed multipart media-create request with native slug, title, alternative text, caption and an exact operation/source marker in its description. Ghost uses its fixed image-upload endpoint with `purpose=image` and an echoed `ref`. These shapes follow the [WordPress media reference](https://developer.wordpress.org/rest-api/reference/media/) and [Ghost image upload reference](https://docs.ghost.org/admin-api/images/uploading-an-image/).

Both use the same bounded JSON response handling, public-DNS/TLS pinning, current binding/credential checks and no-redirect transport as CMS reads. A response must match the operation/source reference and, for WordPress, the native attachment identity/type/MIME. Only an explicitly bound public/admin HTTPS storage path is accepted; external storage requires separate confirmed-origin support. Acknowledgement never claims public image-byte verification, post approval or a live article.

This is a low-level transport, not a wired upload endpoint or completed media stage. The next caller must reserve each immutable image intent durably before upload, validate original paid artifact consent before every effect and fence results afterward. Unknown upload outcomes require a bounded provider-specific recovery policy. The transport performs no automatic retry. Full media staging, native metadata selection, exact final HTML/preview approval, publication/live proof and disposable real-provider CI remain required. No actual image upload or provider effect was executed; all transport boundaries in tests use synthetic images and fake pinned HTTP connections. Rollout flags remain off.


## Public article observation (implementation continuation)

`cms_live_evidence` observes only a confirmed published post from the immutable post intent. It requests the exact acknowledged public URL over pinned public HTTPS, without the CMS credential, cookies or referrer. Redirects, non-200 responses, compressed/non-HTML replies and bodies over 2 MiB do not count. WordPress plain `?p=ID` permalinks retain only the exact post-contract-validated query; DNS validation remains query-free.

The public document must have one absolute canonical link in its head matching the exact post URL and one marked article body. Its copy, heading sequence, links, image sources and alternatives must match the approved native HTML. The exact post headline must be present in the public document without an explicit hidden/inert flag; the CMS theme can supply that headline outside the content fragment so native posts do not need a duplicate H1. Native wrapper/classes/srcset additions are allowed. The versioned semantic profile also normalizes standard prose typography such as smart quotes, dashes, ellipses and numeric multiplication, matching [WordPress wptexturize](https://developer.wordpress.org/reference/functions/wptexturize/); code samples retain a separate exact digest so typography equivalence cannot mask changed code. Missing or duplicate body markers, active markup, explicit hidden/inert content and substitutions are refused. The receipt stores semantic digests and original scope/intent/post identity, not HTML or credentials. Closed validation refuses rebound/unknown fields, boolean/integer coercion and observations older than 15 minutes or more than 30 seconds in the future.

The receipt is `cms_public_article_observation` / `body_verified`; it is not publication approval, a browser rendering result or a completed goal milestone. The future publisher must obtain this observation under fresh private paid-artifact approval/current CMS consent and fence it afterward, then combine required provider-specific proofs. Private receipt ownership remains mandatory: a checksum on worker JSON grants no authority. This helper is not wired into a live publishing route, and no actual public URL or CMS was contacted in these tests.
