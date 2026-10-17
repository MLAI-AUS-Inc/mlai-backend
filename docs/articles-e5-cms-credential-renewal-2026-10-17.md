# E5: renew the original CMS credential — 17 October 2026

Connecting a replacement credential previously disconnected the existing native
connection and invalidated the original paid article's envelope. A new setup
could then abandon the saved payment, draft or unresolved publication.

For a still-connected identical company/owner, platform and public/API origins,
the explicit owner connection action now verifies the replacement credential's
actual native identity outside database locks. Only the same WordPress principal
or Ghost integration, with actual publishing permission, may renew. The original
private key, configuration revision and encrypted payload are fenced again under
the organisation lock. A concurrent owner, policy or credential change refuses
the late result.

The replacement is sealed for the actual original binding and saved in its same
private credential operation. Connection ID, generation, selection revision,
publication policy, original hold, paid artifact/outbox, article approval and
single-create provider journal stay intact. No old ciphertext is rebound to a
new UUID. No model, publication, second charge or repository invalidation occurs.
Only original `connect_cms` waits are made due after renewal; manual review,
running leases and cancelled work are unchanged. The next controller wake can
recover the original operation with the new key, subject to its unchanged
authority, budget and financial checks.

The latest identical acknowledged request returns without provider traffic.
An older superseded nonce cannot roll the key back, and the same nonce with a
different secret conflicts. Public acknowledgements expose no credential,
private fingerprint, paid source or fabricated publication proof.

This is credential renewal, not cross-account/site transfer. Deliberate
disconnection, a deleted private key, another principal/platform/origin and
changed ownership cannot revive the old grant. A replacement binding is refused
while original native goals/artifacts/publications still need preservation or
settlement; it cannot silently reserve a duplicate paid first article. A future
explicit transfer path and the owner's credential UI remain release work.

Validation: eight new regressions cover original paid source/envelope/money,
nonce replay/rollback, different principals and reduced permission, concurrent
revocation/rotation, an already-issued unknown post and closed provider identity.
The full 1,386-test website no-database group passes, all 584 modules are assigned
and 828 failure emissions are audited offline. Disposable ORM CI additionally
renews while the original provider POST is issued, keeps one private key and
post, validates the original envelope, replays the new request and refuses the
old request. Its result must be checked on the PR. No local DB, actual provider
request, paid generation, merge or deployment runs. All rollout flags remain off.
