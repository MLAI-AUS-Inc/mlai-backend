# Native publisher schema4 reader

The new reader validates the hosted native setup protocol prepared in CF #1119.
It has no endpoint, supported-release selector, reservation writer, database
mutation, Ready promotion or publishing caller. Legacy/schema3 consumers still
reject this version; parsing alone does not accept a target.

Every expected value is an explicit independently owned server input: original
reservation, current source tuple, current operation/site/target identity,
supported executable/runtime profile, two sealed probe records and authenticated
hosted origin. The parser does not authenticate transport. Missing original
or current state cannot be hydrated from the arriving receipt or today's scan.
The entire v2 input remains nested and unchanged.

The closed wire checks six exact ordered cases: original, initial setup,
independent first/second probes and successive first/second probes. It retains
all existing native route/browser checks plus visible body copy and preserved
layout. Counts, source provenance, probe digests, derived oracle inputs, exact
routes, executable/runtime identity and the receipt checksum must match.
`native_verified` is evidence of that protocol; publication/support/live
authority remains false. Hypothetical probes are not paid delivery or stop/
refund evidence.

The portable reader, schema and shared fixture are vendored byte-for-byte from
CF `d3caadaa0822385dc7db28876a28ef2ae458a128`. The fixture is explicitly labelled
as mocked Git/program/browser execution. It proves transport conformance, not
native execution, a real reservation or any completion gate. CF's separate
actual browser/fault CI remains required before qualifying a release.

`check_native_publisher_v4_contract.py --remote` verifies the three artifacts
and native/pipeline/runtime producer bytes at that one private source commit.
It uses the existing read-only source token, rejects moving refs/remapped paths/
duplicate metadata/changed bytes/missing access, and prints fixed diagnostics.
This source check does not attest to a runtime image or activate a profile.
Offline checking makes no remote-provenance claim.

Validation: 1,131 lifecycle/journey/domain tests pass with database access
denied, including 11 reader/provenance cases with nested mutations. All 555
test modules are fully assigned with no omissions. The failure audit covers
749 production emissions. Offline provenance checking passes and makes no
remote-access claim.

Tests exercise closed objects/types, independently saved state, origins,
self-rehashed contradictions and actual legacy-reader refusal under the
database/network denial harness. The new module is selected in ordinary CI.
Next supply an independently supported release profile, persist original scope,
reserve current execution and atomically promote only the exact accepted target,
then wire actual setup/article/repeat publication. All rollout defaults stay off;
no migration or production/customer operation is included.
