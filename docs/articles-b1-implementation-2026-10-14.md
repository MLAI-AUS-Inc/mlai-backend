# Articles B1 failure contract — backend implementation

A versioned failure keeps one registered causal class, retry policy and semantic
fingerprint from CF through the saved child run into website preparation. A style
failure remains repairable, an exhausted agent budget remains strategy, and only
proved transport/provider outages become transient. Delivery acknowledgment and
reconciliation diagnoses are B5 metadata; they cannot rewrite the causal failure.

## Activation and authority

`ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED` and
`ARTICLES_FAILURE_TAXONOMY_OBSERVE_ENABLED` default to false. Both additionally
require the saved organization's integer primary key in the server-controlled
`ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS` CSV allowlist, which defaults
empty. Request JSON cannot enroll a run. Authority errors use a trusted resource
scope only after the existing backend authority lookup; construction never
performs a database or provider lookup to discover enrollment. Before that scope
is available, the error retains the legacy wire shape.

Pure classification/validation is always available for tests and B5 decoding.
With policy off, registered versioned input uses its exact reviewed v0 code,
class and boolean for scheduling while preserving the received causal evidence.
Observe mode records the classification difference without changing preparation's
legacy retry transition. The legacy constructors retain their exact code,
boolean and JSON shape when both flags are off or the organization is unenrolled.

## Registry, decoder and emitter coverage

All three canonical files come from immutable CF commit
`cc4025be5c7384f5e93f90ef6d5ddef315ffe589` in
`drsamdonegan/content-factory`; `contracts/failure-codes.lock.json` records the
source and exact SHA-256 bytes. The registry contains 538 causal codes plus
separate diagnostic/delivery code inventories. The pure backend normalizer,
classifier, strict validator and reviewed v0 adapter match the canonical CF core.

Strict decoding rejects code/class/retry/fingerprint/action/version/digest
mismatches and divergent nested or preview aliases. Typed semantic fingerprint
material includes registered findings, safe paths, slots, provider operation and
bounded quantities. Free text, credentials, headers, bodies, timestamps and
volatile execution identities cannot affect the fingerprint. Unknown codes map
to the registered unsupported operator sentinel without retaining or hashing the
foreign string; malformed versioned failures stop as a contract diagnosis rather
than falling through to legacy transient inference.

The AST scanner covers 616 production emission sites in every production
`content_factory` Python module plus the two article-generation integration
entry points. Its inventory includes constructor arguments, dictionary fields,
mutations, attributes, constants, finite strings and enum attributes. The
manifest records 93 exact dynamic-boundary audits and 59 exact noncausal sites;
no module, prefix or wildcard exception exempts new emitters. Noncausal categories
include B5 transport acknowledgments and existing financial, measurement and
owner-preference requests. Unknown literals, unbounded dynamic sites and stale
audits fail with bounded source locations, never provider messages.

GitHub boundaries distinguish actual timeout/network/429/5xx/rate-limit evidence
from malformed provider data, platform credentials and unproved access failures.
A generic 401/403/404 cannot assert that the customer must grant permission.
Proved installation suspension or missing reviewed permissions may use the
registered owner action. Live-route transport, TLS and wrong-marker outcomes
have separate reviewed causes. Website preparation preserves the producer's
failure occurrence; local failures bind to the existing operation/lease attempt.
Waiting observations do not charge step failures. Internal unsupported outcomes
produce operator attention without fabricated owner or article-channel actions.
B2/B3/B4 goal runners, strategy ladders and goal-wide counters are outside B1.

## Regression evidence

Use Python 3.11 and the repository's `scripts/test_without_database.py`; it forbids
database/network access outside the explicit fixture boundaries. The dedicated
B1 module has 28 tests. The affected B1 + existing Phase A/permission/incident/
one-click/journey/reliability lane passes 202 tests.

The fail-before comparison uses a read-only `git archive` of base `c7ab8369`.
The same pure decoder, canonical artifacts, regression module and independently
implemented B5 consumer are held constant; the baseline preparation, authority
error and compatibility classifier remain the original production code. It
runs the identical `FailurePolicyTests` and `FailureDeliveryChainTests` selection:

```text
Before: Ran 11 tests in 0.389s
FAILED (failures=10, errors=8)
After: Ran 11 tests in 0.291s
OK
```

The integrated tests invoke actual DRF parsing/authentication, B5 validation and
receipt projection, the saved `ContentFactoryRun`, and real `advance_prepare`,
journey and `prepare_failure`. Only provider/credential, ORM and transaction
boundaries are replaced. They prove the same causal object/fingerprint/occurrence
survives a style/budget stop and a transient retry resumes the original child;
legacy callback effects and a full worker replay are forbidden by the fixtures.
Additional cases exercise real repository and CI readers, safe unknowns,
allowlist/default-off behavior, strict aliases, fingerprint convergence, and
one-byte/pin/provenance failures.

Offline parity reports all 616 emissions audited and exact local digests valid.
The safe in-memory SQLite drift dry-run reports `No changes detected`.
Database-backed tests, migrations and production/customer operations were not run.

## CI provenance prerequisite

CI invokes `scripts/check_failure_contract.py --remote` against the immutable
source commit and compares the exact bytes of all three files. Mutable refs,
missing read access and unavailable private-source data fail the check; local
offline validation explicitly says remote provenance was not rechecked.
`drsamdonegan/content-factory` is private. A read-only contents credential must be
installed as the backend repository secret `CONTENT_FACTORY_CONTRACT_READ_TOKEN`.
The available secret names were inspected; that dedicated secret is absent.
No credential values were inspected, copied or printed. This access requirement
must be resolved for the mandatory remote parity job; offline success does not
replace it.

Rollback is a code revert with both flags off and enrollment empty. There is no
schema migration or production activation in this implementation.
