# Daily article recommendations

Daily channel-backed research sends up to three relevant article options. The
worker ranks measured opportunities by demand, difficulty and ICP/persona fit.
Topics successfully delivered in the previous seven days, including close
semantic variants, are excluded. This rule also applies before expensive
enrichment so yesterday's winners cannot crowd out new candidates. The generic
interactive discovery carryover fallback is not used for daily selection. When
fewer than three eligible topics remain, send the smaller shortlist rather than
repeat recent topics or invent search data. An exhausted pool produces no topic
message; existing operational failure reporting remains internal.

## A founder's new direction

Saving researched islands records a preference event in the owning research
run's `island_research_selection.daily_priority_events`. Custom-topic discovery
requests also contribute a preference. Unchosen islands never contribute.
Recently saved selections from before this release receive one initial event.

The next successful shortlist reserves one place for an eligible topic closely
related to these preferences. A ten-percent ranking boost is otherwise small;
measured demand, availability, declined-topic exclusions, freshness and company
fit still apply. Preference keywords are included in research seeds and retained
through candidate limits. If no eligible result exists, the preference remains
pending rather than inserting an unrelated or unmeasured topic.

Successful `topic_selection` deliveries record the preference IDs actually
represented. Subsequent runs return to normal ranking. Failed sends, a callback
retry and multi-channel fan-out do not consume a preference twice. Pending
preferences expire after thirty days; adding more themes creates a new event.
Islands themselves remain in the normal research pool and continue evolving.

## Three unanswered days

Before scheduling or dispatching research, count distinct local calendar dates
with a successful scheduled topic delivery since the last response. Email/Slack/
WhatsApp fan-out and twice-daily slots count once per date. Failed/bounced sends,
manual test runs and today's deliveries do not count as completed unanswered days.
After three such dates, pause all research automations for that startup before a
fourth day's research, disable its legacy daily-discovery flag, cancel unsent
scheduled slots and stop automatic island refresh/expansion. An already queued
callback cannot send a new shortlist after the pause. Previously requested article
generation and article-ready notifications continue.

Approving a daily option, selecting a topic through research feedback, declining
a topic, or an unambiguous reply from a verified WhatsApp route records activity.
These actions resume an automatic inactivity pause, but never override a manual
pause. Explicitly enabling reminders in settings starts a fresh activity window.
Settings display `pauseReason: three_unanswered_days` and explain how to resume.
No additional pause notification is sent. Existing channel delivery preferences
and consent rules remain authoritative.

## Implementation contract

- `ResearchAutomation.metadata.daily_research` owns engagement and pause state.
- `NotificationDelivery.request_payload` owns actually delivered topic history
  and `daily_preference_ids`; all lookups are organization-scoped.
- Dispatch includes `daily_topic_policy` with recent topics and pending
  preferences, plus the existing stable run idempotency key. One discovery is
  dispatched at a time per startup so the next slot sees the prior delivery.
- The worker returns `daily_research_selection.preference_ids`. The backend
  intersects them with the dispatched preference IDs before recording delivery.
- No model changes or database migrations are required.
