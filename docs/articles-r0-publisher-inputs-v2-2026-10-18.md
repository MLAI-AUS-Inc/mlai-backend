# Publisher-v2 component reader

This adds the backend reader for the closed original-publisher proposal and
component evidence. It has no endpoint, operation allocator, target writer,
enrollment flag or Ready/publishing promotion. The future executing pipeline
proof remains a separate requirement.

The native-v1 seal remains nested and unchanged. The new proposal binds original
connection/repository/epoch, branch/root, setup operation/attempt, site origin,
original Git/tree/inventory, publisher and exact customer workflow checks,
derived-output relationships, probe digests, actual executor/controller/runtime
identity and the proposed marker wrapper. Validation compares these with a
previously reserved original snapshot and the independently supported
executor/wrapper/native-verifier profile. It cannot hydrate missing fields from
current defaults or from the received proposal. The original snapshot includes
the complete candidate digest, so a newly self-rehashed plan is not the same
reservation.

The component reader verifies source, recipe, input and isolation identity,
bounded generated/detail inventories, publication counts and preservation of
the first new detail through the second probe. It always returns
`pipeline_verified:false` and `publication_authority:false`; those probes are
not paid article delivery, stop evidence or refund authority. An authenticated
transport origin comes from the caller's server context and is not a payload
field. The parsing functions themselves do not authenticate a transport.

The fixture contains actual authored Git/Docker component evidence from CF
#1114's CI run `39117612063`, plus synthetic original scope/reservation/profile
data. Its marker-wrapper digest is deliberately a placeholder. It does not
prove that the marker wrapper ran, that a real reservation exists, or that any
customer site is Ready. Existing schema3 readers reject this new component.

Legacy attestation detection now recognizes newer candidate keys even when
null/malformed. Such a target cannot borrow an old current/live receipt. The
matching CF readers refuse downgrade before source execution. Existing v1 and
schema3 wire definitions and their immutable source pins remain unchanged.

`scripts/check_publisher_v2_contract.py --remote` requires exact schema,
portable-kernel, fixture and actual producer-helper bytes at one full private
CF commit, using the existing read-only contract token. Moving refs, duplicate
metadata, arbitrary source paths, changed local/remote bytes and missing access
fail closed without provider text. Offline validation does not claim remote
provenance. New pure tests are assigned to the normal journey selection.

Next implement the actual one-publisher/marker pipeline and its executing proof,
then reserve, verify and atomically promote it under original scope. Keep the
original raw baseline separate from the current publish head, and retain
browser/layout/content/exact-head checks. The static generated-file guard is
unchanged. No migration, production/customer operation or live refund is
included; all four completion gates remain unproved and rollout flags stay off.

Validation: 1,120 journey/domain/activation tests pass under the database and
network denial harness, including 14 new tests with nested mutation and scope
subcases. Test assignment selects 554 modules without omissions, and the
failure-contract audit checks 749 production emissions. The matching producer
passes 2,251 restricted tests; remote source provenance remains a CI check.
