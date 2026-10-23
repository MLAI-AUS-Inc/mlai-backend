> Historical rollout document. The permanent implementation supersedes its
> bot/both switches and comparison finalizer; see [current operations](slack-member-permanent-operations.md).

# Phase 6: Community source comparison and reader fallback

This implements the existing `SLACK_COMMUNITY_SOURCE` modes without adding a migration, consent version, Slack scope or configuration switch. It does not change production settings, certify a live canary, start an observation window or authorize bot retirement. Release and activation require the owner's separate approvals.

## Source modes and privacy

`bot` retains the legacy Community intake and bot history. `both` accepts and deduplicates authenticated callbacks received by either configured app; history still uses the bot. `members` uses member intake and readers. During this transition, a Community channel with a complete, current assessment proving zero eligible readers falls back to bot intake and history and reports `no_reader`. Cooldown, quota, network failure, missing authority, incomplete assessment or exhausting a bounded selector cannot prove zero. Members-only, private, Slack Connect and uncertain source state never gain this fallback.

A current eligible reader positively authorizes the member-source callback
path even when another grant has unknown membership. Such a count is a lower
bound, not a complete assessment. A grant that never joined a channel may have
no membership row after a complete inventory sweep; it cannot veto another
reader's proven access. With no eligible reader and any unknown authority,
callbacks remain pending and cannot fall back to the bot.

Reader eligibility requires the existing consent and current active account/grant/OAuth/connection, workspace, joined membership and read scopes. It is separate from the author's posting eligibility: neither AI consent, a public identity link, verified-device rank nor `chat:write` can be added as a condition for Community readers. Status counts independent eligible accounts/Slack subjects, not devices or callback authorization recipients. Actual provider work rechecks current authority under the existing ordered locks through page settlement; a status count is not a lease. If only the approved Community mapping's metadata freshness has expired, revalidate it with `conversations.info` before reading content. A successful current public, non-Connect, non-archived result can renew freshness; pending edits and reactions remain queued during this check.

Source and reader changes invalidate the history cursor's authority. A bot cursor cannot continue as a member cursor or vice versa. Original Phase 5 send actors, saved request bytes, attempts, ambiguous results and later mutation ownership remain unchanged through source-mode rollback.

## Durable comparison evidence

Receiving app determines source role. App markers on the message describe the posting application and do not establish which app received the authenticated callback. Observation uses the original durable inbox ingress time, independently of processing retries, and remains outside provider JSON. History, direct store calls and forged metadata cannot manufacture sightings. Distinct configured bot and user apps are required to certify two-source coverage.

Comparison evidence belongs only to the exact fifteen approved, enabled Community room/source pairs in the configured workspace. It excludes private/DM/MPIM/Members/Connect/held or uncertain mappings. A server-created epoch binds that cohort and both app roles. Source/cohort/app changes and known coverage gaps invalidate continuity rather than silently reusing a window.

Both callback orders merge their first qualified sightings before content duplicate and own-send echo returns, without replacing the first raw receipt or delivering content twice. A retired bot's ignored policy receipt cannot reserve the shared content key and suppress the later member copy. Ordinary message creates, replies and provider-bot messages form the comparison population. Thread broadcasts and their original reply count once. Edits, deletes, reactions and deliberate own-send echoes have separate diagnostics and do not enlarge the message denominator.

Raw webhook content retains the existing retention cutoff. Purging locks and re-reads each receipt, erases its raw payload and retains only the validated closed comparison object. A bounded keyset scan visits each candidate at most once per purge run, including rows already reduced to metadata. Analytics cannot become authorization, original-envelope proof or mutation-order evidence after raw facts expire.

## Seven-day cutover gate

Start a continuous, verified `both` epoch at S; the earliest eligible end E is S + 604800 seconds. Count unique Slack message-creation timestamps in the UTC half-open interval `[S,E)`. No partial first or last day, predeployment backfill or process/cache heartbeat can stand in for seven observed days.

Select a concrete observation cutoff C at or after E. Finalize from a consistent snapshot after relevant callbacks received before C have drained and unresolved authorization, ciphertext, observation failures, rate-limit notices and unexplained monitoring gaps are accounted for. Censored or unknown work blocks a pass; it is not a missed member message or a denominator reduction. Continue observing afterward. Evidence merged after finalization can supersede the report and invalidate a prior gate, including a pre-cutoff inbox row that became visible or was processed later. An exact duplicate that changes no evidence does not invalidate it. The report measures relative callback coverage observed by C; Slack supplies no final delivery watermark here.

Let B be unique bot-observed messages and U unique member-app-observed messages. Report `only_bot = |B-U|`, `only_members = |U-B|` and `both = |B intersect U|`, per room and overall. N is `|B|` and M is `|B-U|`. The strict overall pass is **N > 0 and 200*M < N**. Exactly 0.5 percent fails; using the union denominator is incorrect. A zero denominator has an undefined ratio, never zero percent. The plan adds no minimum sample floor or extra per-room ratio threshold; all fifteen rooms still need valid coverage and at least two actual eligible readers each immediately before switching.

Status must expose bounded epoch/window/cutoff and coverage state, comparison counts, censoring/incomplete reasons, eligible-reader counts and current fallback reason/continuous duration, including provisional status before seven days. Unknown values remain unknown and hold the gate. It must not expose bodies, actor or recipient lists, private-room analytics or tokens. Best-effort cache eviction cannot produce a passing report.

The finalized seven-day comparison and the prospective fourteen-day member-source monitor have separate continuity bindings. After cutover, a process restart, release/configuration change, monitoring gap or channel reader fallback clears prospective stability proof. Monitoring can resume from the current time after the same fifteen source mappings, app bindings and current readers are verified; it cannot backfill the gap. The historical comparison remains available, with later evidence still able to invalidate or revise it. A posting-author bot fallback by itself does not mean that the channel lost its reader.

## Release, activation and rollback

1. Pass source review, the owning SQLite/PostgreSQL tests and full hosted CI. Obtain exact release approval, deploy normally and verify actual runtime/source, all rollout values, room mappings, database and Studio preservation.
2. Finish the client releases and physical-device checks. Run the separately approved scoped Members and Community live canaries, including privacy, membership/revocation, attribution, mutation, echo and rollback checks. No global activation follows from source release.
3. Enable the explicitly reviewed Community cohort at `both` only after prerequisites pass. Record the actual epoch and configuration coverage. Observe all fifteen rooms for the full comparison interval and finalize the drained report at C.
4. Immediately recheck two independent current eligible readers per room and the strict overall miss gate, then obtain member-source cutover approval. Preserve the reviewed report and actual configuration change.
5. Observe at least fourteen stable days in `members` with no fallback before removing the bot app's `message.channels`, `reaction_added` and `reaction_removed` subscriptions. Reader loss/fallback resets that stability evidence. Bot subscription removal is an owner action; keep its membership and posting scope.

Rollback is the existing `SLACK_COMMUNITY_SOURCE=bot`, using the normal approved configuration/release process. It restores the transitional Community intake/history strategy without relabeling send actors or replaying work. After bot subscriptions are removed, live bot-event fallback cannot receive callbacks until the subscriptions are restored. After Phase 7 deletes bot intake/read code and fixes the source to members, missing readers are an operational hold requiring the supported source/release rollback; neither subscriptions nor bot membership alone recreate deleted code.

The seven-day and fourteen-day gates are elapsed observation requirements. Green tests or deployment health do not satisfy them. Phase 7 cleanup and optional Phase 9 work remain separate.

## Closed operational logs

The `slack_member_event`, `slack_member_channel`, `slack_member_send`,
`slack_public_echo` and `slack_community_source compare` families contain
validated primitive mapping/source/room/delivery/receipt IDs, fixed outcomes
and already-derived comparison counts. Authenticated public routes use the
owned public bridge mapping, not a payload prefix as privacy proof. Personal
DM/MPIM mirrors do not enter these families. Lifecycle holds for formerly
public rooms retain only their existing mapping IDs after archive or privacy
conversion. Send logs never contain the profile header, body or token.

Successful database outcomes are emitted after the enclosing transaction
commits. Logging failures cannot change synchronization. Logging never opens
a database connection; closed connections and uncommitted manual transactions
are skipped. Rollback drops scheduled success logs. Existing status and
durable receipts remain authoritative when operational logs are unavailable.

Comparison observation lines describe a merged sighting transition and identify
message, own-send or diagnostic population; they are not additive counters.
Only report lines contain the existing report's per-room `only_bot`,
`only_members`, `both`, `N` and `M` values. Fallback lines record persisted
source-policy transitions, including complete-zero `no_reader`, without
inventing a new observation window or performing another measurement query.

## Validation ownership

The owning labels are `integrations.tests_community_source_phase6`, `integrations.tests_community_source_codec_phase6` and `integrations.tests_community_reader_phase6` in the canonical SQLite and durable PostgreSQL jobs. Preserve the Phase 4 privacy/authority/history tests and Phase 5 immutable send/echo and PostgreSQL revocation/settlement regressions. Database tests use only the specifically approved existing disposable migration inventory; this document grants no migration permission and records no test result.
