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
a second encoding. Count actual delivered bytes, including renderer options.

Validate with the real run serializer and a synthetic 24-run company carrying
large review artifacts. Record response sizes and rendering durations, check
that full details remain obtainable explicitly, and assert that neither view
invokes GitHub refresh. Use only the repository's database/network-denying
test harness locally. No migration or production operation is involved.

The local 24-run stress fixture gives each run a two-million-character review
document plus large raw article/diagnostic/manifest values. The real serializer
produces 110,695,470 bytes for the full run list and 49,374 bytes for the summary
list. One measured run took 405.00 ms for full serialization and 2.83 ms for
summary serialization. These are synthetic run-list measurements; they exclude
database, authentication, bootstrap topic/capability projections and transport.
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
