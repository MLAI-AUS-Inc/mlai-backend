# Hosted publication authority

A hosted publication retains its original site, run, approving actor and reviewed
revision. Admission and progress callbacks lock the organization before the site
and publication, sharing the team membership/offboarding lock. The server then
reads the current active actor and owner or explicit member publish grant. It
rejects frozen teams, stopped runs, changed sites and substituted approvers before
changing publication or run state. A pending request cannot be adopted by another
publisher.

Every rendering callback requires current permission, including a replay of the
same rendering state. Content Factory treats a successful rendering response as
admission to start the hosted build, so a revoked callback must be refused. The
actual worker stops before the builder when that response is forbidden.

An exact already-applied live callback is a history acknowledgement. It may return
the existing publication after grant loss, cancellation or a pause, without
changing content, deployment, clocks or run status. New content, revision or
site data requires current authority. Existing live pages and reads are retained;
a grant change does not unpublish them.

A rendered revision cannot change while a publication is rendering, deployed,
live or failed. A new owner-reviewed pending request is required before replacing
that revision. The automatic proxy activation also repeats the current owner’s
publish-rights check inside its existing final organization lock, preserving its
exact owner click and merged-head requirements.

These checks do not debit or refund points, replace original charge lineage,
re-read or rewrite the saved approved article, or change migrations. Acceptance
coverage uses synthetic service responses and isolated UTF8 PostgreSQL, including
member removal racing the publication admission lock.
