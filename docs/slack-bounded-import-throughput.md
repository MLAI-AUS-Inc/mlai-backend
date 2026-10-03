# Bounded Slack import and throughput

Public history repair imports the most recent 30 days. Archive and thread
requests carry that lower bound, and page writes independently enforce the
rolling cutoff. Existing unbounded public cursors restart with the bounded
query. A descending main-history page reaching the cutoff ends that scan;
ascending thread pagination continues past an old root to eligible recent
replies. Recent replies whose parent is outside the selected window appear as
standalone messages, without importing the older parent body.

Private history continues to use each owner's selected consent window. Known
thread roots can be older than that window: the source root identifier locates
the thread, but only replies within the window are imported. This does not
discover every unknown old thread that gained a recent reply. Source callbacks
and existing thread locators cover known activity; a bounded main-history query
alone is not proof of complete historical thread discovery.

With durable message sync enabled, a private archive scan owns initial thread
pagination. Its independent thread-repair job waits rather than fetching the
same replies concurrently or immediately repeating them. Full-window private
reconciliation runs daily. Continuous source events, recent-head repair,
independent thread repair, and membership/consent verification continue. A completed old-root thread scan with no recent messages
waits a day before rechecking; an old root with recent replies retains hourly
repair. Legacy mode retains hourly full-window reconciliation.

## Adaptive recent-history repair

The first recent-history scan still covers the preceding day within the selected
history window. After a complete, unrestricted source scan, the next routine
scan starts five minutes before that scan's upper bound. This watermark moves
only when every page has committed under the existing lease and authority
checks; partial, failed and source-limited scans cannot advance it. A worker
returning after an outage scans the full gap within consent, including gaps
longer than one day. Mapping, participant audience or consent-window changes
invalidate the old incremental boundary. No message body is stored in this
checkpoint, and it never determines the user's read cursor.

The default retains the one-minute repair cadence. Only when
`MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED=true` do successive quiet scans wait
two, four, eight and then at most fifteen minutes. Verified source events and
authorized foreground hints wake existing mirrors without replacing active
leases, pagination, owner fairness or provider retry delays. Event delivery and
the existing explicit conversation-open refresh remain independent of this
background timer. Every six hours the head scan includes the preceding day
again to catch older edits and known thread roots; existing full-window archive
and thread reconciliation remain in place.

The fifteen-minute cap is deliberately conservative until callback reliability
and recovery latency have been measured. It reduces idle work but does not
prove capacity for every hundreds-user workload. The true repair interval can
still exceed its target when shared method budgets are saturated.

## Foreground service and measurement

The existing app/workspace/method budget remains the only provider allowance.
Recent foreground demand limits competing background admission to half that
allowance; idle background work can borrow the whole allowance. Provider
`Retry-After` always wins, and adding replicas cannot multiply capacity.
Foreground requests are not capped by this preference: continuous foreground
saturation can still delay background completion. The import-share estimate is
a planning assumption, not a guaranteed reservation.

Open-chat head checks, new activity/read hints, and discovery needed to validate
staged live private messages retain foreground priority. Opening a conversation
does not promote its entire archive or thread-repair queue. Public wakeups are
optional, bounded and skip busy state rows so they do not hold up cached reads.
Client startup, rendering, subscription limits and consent-selected history are
unchanged by these scheduling changes.

`message_sync_status --window-minutes 15` reports recent provider counters;
`--window-hours 24` uses completed hourly buckets retained for seven days.
Telemetry is best effort and excludes bodies, tokens and user identifiers.
Missing bucket coverage is unknown, not zero use. Combine provider counters
with oldest queue age, actual delivery latency, worker progress, host memory,
database pool waits and device startup/scrolling measurements.

`message_sync_capacity --owners 300 --mirrors-per-owner 20
--repair-interval-minutes 15` models a lower bound of 400 history requests/minute
before active chats, thread repair and imports. This exceeds a configured
50/minute allowance. Adaptive repair and more server RAM alone therefore do not
establish a 300-user capacity guarantee; measure actual mirrored conversations,
event coverage and competing traffic before expanding onboarding.

## Rollout and rollback

1. Deploy with quiet backoff disabled. Record comparable baseline and candidate
   foreground delivery/startup latency, queue ages and provider deferrals.
2. Verify public, DM, group-DM and private-channel callbacks for the correct
   app/workspace, including edits, replies, reconnect recovery and revocation.
   An aggregate healthy heartbeat is not evidence of every callback path.
3. Use the isolated relay capacity harness in `mlai-chat/perf/RELAY_CAPACITY.md`
   for 300/500 synthetic-client fan-out and private-audience checks. Its offline
   tests are not measured relay capacity. Compare the actual mobile experience
   separately; require no material startup, scrolling or delivery regression.
4. Enable quiet backoff for a monitored release only after those gates pass.
   Watch for lost/duplicate delivery, rising backlog and source recovery delay.
   Stop expanding onboarding if the offered request rate exceeds the budget.
5. Disable `MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED` to restore the one-minute
   scheduling policy. Already scheduled quiet jobs may take up to fifteen
   minutes to run once; active hints continue to wake eligible jobs. Reverting
   the release restores the previous scheduler without a schema migration.

Do not shorten a user's selected history window to meet a throughput target.
For eventual migration away from Slack, serve native chat through the existing
relay and reduce bridge obligations as users explicitly disconnect. Separate
bridges or Slack plan upgrades do not create more allowance for the same
app/workspace/method.

## Capacity and the one-to-two-hour objective

Slack budgets are shared by app, workspace, and method, across all users and
workers. At a configured 50 requests per minute, each method has capacity for
3,000 requests in one hour or 6,000 in two hours, before retries and ongoing
maintenance. These are request counts, not conversation or message guarantees.
See [Slack rate limits](https://docs.slack.dev/apis/web-api/rate-limits/) and
[conversations.history](https://docs.slack.dev/reference/methods/conversations.history/).

Directory discovery can dominate onboarding: `users.conversations` does not
provide an activity-date filter, so an unknown conversation may need its own
metadata request before it can be excluded as older than the selected window.
For example, 1,256 such requests need at least 25.1 minutes of the shared
50-request/minute budget; 6,433 need at least 128.7 minutes. History pages,
thread replies, membership pages, source throttling, and competing users add
work. Adding workers or tokens cannot increase the shared Slack allowance.

A one-to-two-hour import target must therefore be measured against supported
workspace volume and concurrent onboarding load. Completion requires directory
exhaustion, selected-window main and thread coverage, successful relay delivery,
and a source read-state snapshot. A ticking scanned counter or healthy worker
heartbeat does not establish completion.

## Device enrollment boundary

Private relay rooms currently use the exact participant-key set as their
identity and signed delivery audience. Adding a verified device changes that
set and requires a new room. Completed backend delivery bodies are cleared, so
the current backend cannot republish their history from an existing plaintext
cache. Avoiding source refetch requires a coordinated owner-conversation/device
audience design or a separately authorized encrypted replay store; simply
skipping the reset would break history availability or audience verification.

Regression coverage lives in
[`integrations.tests_message_sync_history`](../integrations/tests_message_sync_history.py).
