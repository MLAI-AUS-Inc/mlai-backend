# E3 original inventory count disclosure

`ARTICLES_EXISTING_INVENTORY_DISPLAY_ENABLED` defaults to false. Together with
the existing website-status rollout, it adds a bounded original capture summary
and “We found N existing articles” evidence to `websiteStatus`. It does not
change a goal state, milestone, next action, capability, ownership or approval.
Missing evidence stays missing; a valid empty capture is explicitly zero.

The producer is Content Factory draft PR
[1063](https://github.com/drsamdonegan/content-factory/pull/1063). Its immutable
commit and exact schema/example bytes are pinned in
`contracts/native-inventory-summary-v1.lock.json`. Existing CI verifies both
files through the existing read-only contract secret. Local verification checks
only vendored bytes and makes no remote-provenance claim.

The consumer reads the preparation's exact canonical setup child, scoped by
organization, website, repository, consent generation, original operation and
deletion watermark. It does not search for a latest run or fall back to config
counts. Root/nested callback copies must agree. The closed summary then binds
the carrier run, branch, app root and site origin; source identities and digests
are validated, sizes bounded and credential-shaped text rejected. The original
observation's SHA remains historical when the selected branch advances.

The public projection contains only count, listing route, observed SHA, carrier
run and inventory/evidence digests. Record bodies and repository file paths stay
out of the display contract. Disconnection adds no count. Disabled rollout adds
no child read. Rollback is the same flag; no migration or provider operation is
required. The Chat companion must consume this display field without turning
it into publishing readiness.

Validation: 36 database-free source/status/doctor/goal cases and three immutable
contract-parity cases pass. All 514 test modules are fully assigned in CI. The
692-emission canonical failure audit, Python compilation and whitespace checks
pass. No local database, migration, production credentials or deployment was
used. Existing remote CI still needs to validate the final PR head and pinned
private producer access.
