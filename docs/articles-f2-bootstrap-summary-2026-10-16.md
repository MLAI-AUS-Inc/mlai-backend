# F2 bootstrap response design

The bootstrap already supports compact run serialization, but a request without
`view=summary` selects the full response. That path includes recent review HTML
and refreshes GitHub status while reading the dashboard. The timing helper also
encodes the payload once to count bytes before DRF encodes it for delivery.

Make summary the default bootstrap contract and retain explicit `view=full`
for callers requesting complete data. Run details keep their existing full
endpoint and remain the source of review HTML and raw diagnostics. Bootstrap
reads must not refresh GitHub or perform publication reconciliation, including
the explicit full view. Existing authenticated mutations and background status
reconciliation retain their ownership and authority boundaries.

Record response bytes and elapsed time from DRF's first negotiated render.
Cache that rendered content on the response so measuring it does not introduce
a second encoding. Count negotiated, uncompressed response bytes, including
renderer options; compression and transport overhead are outside this metric.

Validate with the real run serializer and a synthetic 24-run company carrying
large review artifacts. Record response sizes and rendering durations, check
that full details remain obtainable explicitly, and assert that neither view
invokes GitHub refresh. Use only the repository's database/network-denying
test harness locally. No migration or production operation is involved.

The local 24-run stress fixture gives each run a two-million-character review
document plus large raw article/diagnostic/manifest values. The real serializer
produces 60,535,614 bytes for the full run list and 49,638 bytes for the summary
list. One measured run took 192.91 ms for full serialization and 2.05 ms for
summary serialization. These are synthetic run-list measurements; they exclude
database, authentication, bootstrap topic/capability projections and transport.
These corrected measurements use the registered `article_generation` workflow;
the original generic `article` fixture bypassed article-specific projection and
overstated the full response size.
The database/network-denying harness passes 25 bootstrap, merge-fact, portable
control and private-preview tests. Renderer coverage counts all JSON encodes,
includes negotiated indentation/Unicode bytes, and verifies repeated render
access does not encode again. Both read views reject opportunistic GitHub work.

Chat's bootstrap client previously omitted the `full` query value. Its paired
change sends the selected mode explicitly and defaults to summary, preserving
explicit full requests against either backend version and matching the snapshot
cache's selected mode. Its regression checks actual outgoing company/view
parameters and the account/company/view snapshot identity. Full run-detail
endpoints retain their existing contract.

The remaining F2 work includes overlay caching that preserves immediate
per-user authority/revocation checks, end-to-end Chat lazy-detail validation,
and measurement of the complete endpoint against the 300 KB / one-second p95
budget. A local serialization benchmark is not production latency evidence.

## Complete read path

Capability projection still reaches account-liveness and repository-HEAD probes
on a cache miss. Bootstrap must select an explicit observation-only mode in both
views. Shared capability projections also default to observation-only, covering
the website-status GET and MCP capability read. Existing forced admission and
background callers select probes unless they explicitly opt out. Observation
mode reads finite existing probe results, checks current actor and
connection scope, and returns an unverified observation when none exists. It
must never mint credentials, perform HTTP, or acquire a probe lease. Explicit
admission and background verification retain their current probing behavior.
Repository observations must include the live connection state, repository,
installation, branch, generation and configuration revision in their identity,
so a cached positive observation cannot survive a changed or revoked binding.

Load the current config and recent runs once for each bootstrap request and
reuse them for capability and workflow projection. This is request-local reuse;
it does not cache actor authority across requests. A cached payload still gets
current connection, capability, journey and goal status. Check actual cold and
warm endpoint responses with 24 large persisted runs in the existing disposable
CI database, including total encoded bytes, SQL count and elapsed time. Run only
database/network-denying unit tests locally. Broader overlay caching and SQL JSON
projection follow measured endpoint evidence; they cannot weaken scope checks.

## Cache workflow presentation within the live overlay

The corrected 24-run endpoint fixture passes its byte/time assertions in the
disposable PostgreSQL lane at `d1087325` (complete job logs remain pending).
Next cache the root and per-run workflow progress, which repeatedly reads
component feedback. Current website summary, Google status/connect URL,
capabilities, journey and goal status still refresh on every request.

The presentation cache is partitioned by organization, actor, config update,
current connection identity/update, current capabilities/checks, topic count and
displayed run IDs. Include aggregate count and latest update for all organization
runs and component comments so source revisions and feedback outside the visible
window invalidate it too. Cache only presentation dictionaries, with a maximum
20-second TTL and deep-copy each use; never retain ORM objects or credentials.
Setting the bootstrap TTL to zero disables this cache as well. Cache failures
fall back to the normal projection. No cached presentation grants an action;
admission rechecks live authority and source proofs.

Validate warm reuse, immediate invalidation for changed feedback/run/connection
facts, cache failures/disablement and current permission fields. The existing
endpoint fixture continues to enforce full response bytes, no HTTP/credential
minting/database writes, and the synthetic one-second p95 bound.
