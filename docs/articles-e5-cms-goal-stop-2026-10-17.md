# E5: stop original native generation before unused-work credit

A local cancelled run is not proof that CF stopped. The native financial worker
now claims a durable lease, releases organization/row locks, resolves the
original backend dispatch key and obtains a precise CF no-work or cancelled
observation. Unknown keys, absent/generic success replies, another run ID or
pending cleanup retain the hold and schedule another observation. A lost reply
can repeat the same idempotent CF cancellation. The result cannot overwrite a
newer financial-worker lease.

After HTTP, the worker locks the organization and original financial rows again,
rechecks the inactive original goal/dispatch/payment, and saves a minimized
private stop receipt in that original outbox. It binds the exact goal, billing
key, request digest, remote run and observed outcome. It then cancels further
backend delivery and settles the original credit. A late worker terminal
callback defers to this coordinator even when it omits/changes goal fields.
Actual already-committed credit still wins stale snapshots. Missing private stop
proof remains pending; malformed saved proof requires financial attention.

The worker needs no current CMS key, live owner session or enabled execution
rollout. A hold with no original outbox could not have been dispatched through
the canonical transaction and needs no remote cancellation. Accepted useful
drafts retain their charge and CF handoff without cancellation: CF cancellation
clears artifacts, which could destroy the source needed for GET-only recovery of
an uncertain provider post. A draft arriving during the stop claim causes a
retry and a fresh retention decision.

Validation: all 1,166 no-database website checks pass, with eight original-key
cancellation cases and two additional coordinator-race cases. The focused
financial/cancellation group passes, including transaction rollback, lost credit
acknowledgements, revocation, erasure and existing credit. Model/migration dry-run
reports no changes. Disposable full CI remains pending. All rollout flags stay
off; no actual CF/CMS call, wallet service, paid generation, local database,
merge or deployment ran. Native goal start/controller and Chat integration
remain separate implementation work.
