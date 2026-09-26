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
independent thread repair, and membership/consent verification retain their
existing cadence. A completed old-root thread scan with no recent messages
waits a day before rechecking; an old root with recent replies retains hourly
repair. Legacy mode retains hourly full-window reconciliation.

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
