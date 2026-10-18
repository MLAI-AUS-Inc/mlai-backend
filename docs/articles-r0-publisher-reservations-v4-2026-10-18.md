# Original-publisher native setup reservations

Schema4 receipts now have a durable backend state boundary on the existing
website operation rows. This is setup acceptance; the full original-publisher
article and repeat-publication path remains a required next step.

The independent `ARTICLES_PUBLISHING_INPUTS_V4_ENABLED` switch defaults to false.
It also requires hosted attestation/execution and original-goal enrollment.
`contracts/native-verifier-v4.json` ships an empty profile list. No deployed
runtime or authored CI image is silently enrolled. A future reviewed profile
must bind exact native/controller/layout/runtime and executor identity to the
immutable CF source lock and a qualification digest. Only `setup_only` profiles
are accepted by this version.

The original reservation is allocated under the latest consented prepare goal.
It checks the independently saved setup run and its actual workflow operation,
original request/attempt, connection/repository/generation/deletion epoch,
selected target, site origin and configuration revision. A trusted server-side
source observer reads the original immutable commit outside authority locks.
The exact raw commit/tree/full-inventory tuple is then saved under a repeated
scope fence before any candidate can freeze it. An existing reservation replays
without another observation or timestamp change. Missing original history
cannot be filled from a candidate, receipt, scan or current default.

The second phase validates the entire v2 candidate and two sealed hypothetical
records against that reservation, its selected target and supported profile.
Another server-side source observation supplies the current setup commit/tree/
inventory. It allocates one stable child operation and queued run, retaining
the original parent and saving the exact identity, request, profile and both
source bindings before dispatch. Retries reuse the child and saved inputs;
they do not renew the goal budget or create another points charge. The child
is retained in the parent's bounded usage history, carries original-goal cost
attribution and receives a frozen remaining-budget projection. Unknown usage
and full history remain held; provider usage is retained on proof acceptance. An enrolled
target's stronger proof requirement is persisted and publishing is withheld.

The existing attestation entry routes typed schema4 receipts to the new writer.
It validates against saved state, checks the current Git head, reads only the
reserved child's fixed authenticated hosted status route outside transactions,
compares the service's proof with the submission, and repeats all original/
current/target/operation/profile fences before writing. Target, selected config,
original operation, child receipt and run result commit in one transaction.
Concurrent identical results return the same child. A committed replay performs
no provider read and does not renew any proof or model timestamp. Changed
consent, selection, source, run, operation or profile refuses acceptance.

The saved result is `setup_verified`, with `allowed=false` and
`publishingReady=false`. It cannot satisfy the existing legacy/schema3 Ready,
CI, live deployment or paid publication gates. It does not produce article,
refund or stop authority. Existing required-proof guards preserve the accepted
setup and config from weaker subsequent scan proposals.

Source observers are internal trusted callables, not request parameters. Their
fixed CF original/current observation transport and normal setup dispatch caller
are still to be connected. The current CF hosted job is still the prior native
protocol and cannot yet execute this newly reserved request. No claim is made
that the end-to-end v4 workflow is dispatched. Next add that authenticated
producer transport/configuration, wire setup/adoption and the actual original
publisher through preview/CI/article/repeat publication, then qualify an exact
compatible release and activate Ready only after all those consumers support it.

Validation: 1,144 lifecycle/journey/domain tests pass with database, network,
listeners and subprocesses denied, including 13 new pure tests with nested
profile/source/proposal/binding mutations. All 557 test modules are fully
assigned, and 754 production failure emissions pass the offline audit. New
PostgreSQL tests run only in disposable CI and cover the real writer, durable
retries, missing history, source-observation races, cancellation/consent/target/
selection/profile/run races, interleaved duplicate completions, saved scan
protection and rollback after every earlier write has executed. No migration,
deployment, live refund or customer/provider mutation is included. All four
completion gates remain open.
