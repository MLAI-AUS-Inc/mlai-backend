# E5 / v2 3.2: one publishing-channel interface

The owner's instruction to continue the implementation authorizes the design and
code preparation. Keep rollout off and use draft PRs. No provider account,
credential, customer operation, deployment or publication is authorized here.

## Boundary

Expose exactly `inspect`, `capture_existing`, `prepare`, `verify`, `publish`,
`unpublish` and `disconnect` through backend-owned channel implementations for
`git`, `cms`, `hosted` and `portable`. The common dispatcher chooses from the
current saved company/site channel preference or a server-reserved goal binding;
repository JSON, worker callbacks and article payloads cannot choose an adapter or
supply executable handlers. A detected CMS is not authenticated CMS authority.
`publish_code`, `review_draft` and `content_only` remain inner Git delivery modes.
Portable generation continues to use the existing explicit content-only route.

A channel result distinguishes observations, pending work, owner action,
unsupported capability and completed work. It carries only bounded fixed fields
and existing run/operation/artifact references. A portable export is an artifact,
not a live article. Read-only inspection/status cannot call a writer or scheduler.
No channel can turn an informational recommendation into provider proof.

## Keep the existing controls

Git wraps the existing company-scoped scan/setup, CI/live verification,
approval/publication and disconnect handlers. It retains original source,
connection/generation, owner approval, budget, idempotency, merge policy and live
content checks. The goal runner and article pipeline route through the interface
only when its independent rollout is enabled; feature-off remains the same flow.
The strategy ladder's hosted rung must select the hosted implementation and must
never fall through to another Git setup attempt. A handler that is unavailable
returns an explicit bounded blocker with hosted/portable options.

Each effect uses a current backend-owned reservation and repeats its channel and
site authority check immediately before the external call. Rechecking a receipt
is not an authority lease. A generation/domain change cancels future effects.
An original Git run retains its own provider/approval identity after a later
portable preference; it cannot acquire CMS/hosted authority. New preferences
route new authoring intents; explicit disconnect revokes existing site authority.
Uncertain provider acceptance reuses the original idempotency/reference
and reconciles instead of creating another post. Disconnect preserves published
content unless the owner separately requests an implemented unpublish operation.

## Non-repository authority

CMS and hosted execution must not fabricate GitHub repository IDs or reuse GitHub
credentials. Reuse the existing WebsiteConnection/operation storage only with an
explicit backend-owned non-repository discriminator and a separate channel guard;
legacy Git/token guards must refuse that discriminator. Current schema permits a
null repository ID and empty repository name, so this needs no migration. A new
non-repository binding still belongs to the selected founder/company/domain and
has a monotonic generation and explicit consent. Its provider receipt/credential
reference is private, independently encrypted and absent from projections,
callbacks and repository files. Actual credential binding belongs to 3.3; hosted
origin selection/provisioning belongs to 3.4.

The 3.2 implementation must not present a stub connector as working. Until each
provider is implemented and tested, its effect capabilities are unavailable with
a plain reason. The common fake contract tests exercise all seven operations,
original-request preservation, provider isolation, stale selection/generation,
unsupported operations and portable/live distinction. Actual Git zoo acceptance,
CMS container cycles and hosted-domain acceptance remain required separately.

## Delivery sequence

Implement the typed result/operation contract and server-owned registry, then Git
and portable wrappers plus existing goal/article/status integration. Add explicit
non-repository binding/guard tests before introducing the 3.3 credential writer.
Use existing failure classes and keep all new flags off. Run the complete assigned
website group and the contract/emission audit; use only disposable remote CI for
actual database/locking and CMS-container tests. No local database or compiler.

## Current implementation and remaining acceptance

The typed request/seven-operation vocabulary, closed backend registry, local status
inspection, existing Git prepare/scan/CI/live/first-article/publication/disconnect
wrappers and explicit portable-generation wrapper are implemented behind
`ARTICLES_PUBLISHING_CHANNELS_ENABLED=false`. Original inner delivery modes,
arguments, receipts and guarded results remain unchanged. An exhausted hosted
strategy cannot launch another Git setup. Unsupported provider effects and Git
unpublish do not report completion. CMS/hosted provider bindings, a full portable
artifact lifecycle, non-repository goal proof and CF's outer-channel envelope are
still required before this step can be treated as complete or rolled out.

Local evidence: all 859 database-free website cases pass, including eleven new
interface cases. One case reruns six existing first-article scenarios through the
enabled interface (dispatch journaling, required review, lost acknowledgement,
quality-gated approval, stale source/cancellation and live-deployment wait).
Provider-isolation, explicit portable/no-repository-authority, original Git-run
intent and private-request repr cases also pass. The 730-emission audit retains
the exact unchanged dynamic-emitter declarations under their extracted helper
names. All 539 modules are assigned. This is controller/contract evidence, not a
live fixture or provider acceptance claim.
