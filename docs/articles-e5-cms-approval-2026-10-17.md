# E5: exact native CMS article approval

The default-off CMS artifact pipeline records one private `cms-approval`
operation per exact staged revision and original native publication policy.
An owner who chose review confirms the final preview once. Automatic approval
requires the private `cms-policy` operation sealed into the original generation
envelope before its original spend. Git preferences, worker approval flags,
copied receipts and later policy changes cannot substitute for that consent.

## Owner API

The existing authenticated, company-scoped website action route accepts:

- `cms-review-article`: current connection tuple plus either `stage_id` or the
  original `source_run_id`; returns `cmsArticleReview` containing stage ID,
  revision, title, inert final `previewHtml`, status and `approvalRecorded`.
- `cms-approve-article`: current connection tuple, exact `stage_id`,
  `expected_revision` and literal `publication_confirmed=true`. It returns 202
  while the durable checks are pending, or 200 for the same completed choice.

Both responses are private/no-store. The selected founder/company and current
CMS binding are checked before looking up private article bytes. Preview HTML
must be displayed in the client's existing sandboxed preview boundary; it is
not executable application markup. These endpoints expose no API origin, key,
credential reference, payer or spend record. Normal operation/detail/status
reads continue to exclude all private native approval payloads.

## Proof refresh and recovery

The approval intent binds the original source/handoff, stage/body/title/preview,
current binding/credential, policy/native author/category/image placement and
original billing lineage. A short durable lease rechecks the exact current CF
handoff outside locks, then fences the result against current ownership,
generation, cancellation, policy and original spend/refund state. Expired
leases can be replaced; an old reply cannot overwrite a newer worker or owner
cancellation. Unavailable CF reads use bounded backoff and shared failure
evidence. No model call, charge, provider write or Git proof is introduced.

When owned-image observations age, `public_recheck` retains the historical
proof and acknowledgement while the same public URLs are read again. It keeps
the sole upload allowance and final preview revision unchanged. The original
owner choice waits and resumes automatically, including through transient media
reads; no second click or upload is needed. Historical stage reads never grant
fresh-media authority. Completed approval consumers re-resolve the private row,
policy, exact stage, original payment and fresh public images by default.

The scheduler filters original automatic stages before its bounded limit and
excludes already reserved revisions. Review-mode stages cannot starve automatic
ones. Disabled jobs remain frozen and never fall through to generic Git cleanup.
Disconnect/purge erases private approval payloads with the other CMS operations.

## Validation and remaining work

The 1,060-check no-database website group passes, including 13 new approval
cases. It covers exact manual/automatic consent, late replies, crash leases,
media aging and transient refresh, bounded reads, changed CF bytes, refunds,
changed policy, owner scope, forged receipts and default-off behavior. Model
checks and migration dry-run report no changes. The failure contract audits 777
production emissions, and all 557 test modules are assigned. The existing
disposable-CI connection regression additionally checks actual automatic
reservation/deduplication, approval/readback, original-run preview selection,
refund denial and purge. Its database execution is remote-only and pending CI.

All rollout flags remain off. Durable post publication and public-proof/goal/UI
integration, paid revision lineage, unpublish and real WordPress/Ghost acceptance
remain required. An approval receipt is not a publication or live-site proof.
