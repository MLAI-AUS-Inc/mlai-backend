# Personal Maek and Maek · community

Personal Maek is the owner's existing OpenAI Dot. Its account, OAuth grant,
connectors, memory and owner-only signed mention admission remain unchanged.
Guests cannot invoke it. There is no guest-to-personal fallback.

Maek · community is a different assistant, not a restricted view of that Dot.
It has its own generated signing key, installation (`mlai-community`), grant,
owner proof and `community-public-thread-v1` consent. A verified MLAI member can
mention its chip in the one approved public stream. Both assistants' replies
are visible to members of that channel; personal invocation is not a private DM.

## Request flow

1. The owner adds the community assistant in Apps & agents, selects the approved
   channel and signs the distinct consent. Names must end with ` · community`.
   This does not consume the owner's personal Dot slot.
2. Enrollment publishes the separately signed kind 0/10100 profile through the
   existing native gateway. Its directory type is `community`, with `respond_to`
   set to `anyone`. Relay registry classification binds that key to this app.
3. `run_community_agent` polls signed mentions through the existing native
   gateway, in community execution mode. The relay checks the owner, guest,
   active public channel, moderation restrictions and current trigger.
4. Backend admission requires an active, verified MLAI caller device/account.
   Each installation admits at most its grant's daily request cap (20 by default),
   with a five-request cap per caller account across all its devices. Requests
   expire after 15 minutes. Limits use the native configured day/timezone.
5. The dedicated worker reserves an existing AgentTask budget before inference.
   Each call contains a fixed policy and at most 64 KiB of public thread data
   ending at the trigger. It contains no Dot tokens, memories, connector tools,
   private chats, agent memory store, saved provider conversation or prior
   response ID. External media and links are not fetched. The model receives
   `tool_choice: none`, no tools and `store: false`. This is not a claim about
   all upstream provider retention; the configured provider's policy still applies.
6. After inference, the worker rechecks the grant, caller, lease and live trigger.
   It signs one text-only reply with the community key. The existing relay
   gateway repeats source/membership/moderation checks at final storage.
   Retries resend the exact signed event. They do not generate another answer.

The worker is separate from personal Dot delivery. Generic agent task workers
cannot claim or recover community tasks. Interrupted inference is failed rather
than automatically repeated, preserving its conservative budget reservation.
Failed/expired requests are not automatically sent to the personal Dot. The
owner can see correlated first-reply evidence; worker errors have redacted
stage diagnostics. This pilot does not add guest-facing per-message error UI.

## Configuration and rollout

Both new gates default to false. This change adds no migration and does not
activate, re-enroll or change the permissions of any existing installation.

1. Review/test the chat and backend PRs. Deploy the relay implementation before
   enabling backend community execution. Do not enable against an old relay.
2. Configure the existing operator-owned inference transport in the backend
   host secret environment: `AGENT_RUNTIME_MODEL_BASE_URL` (HTTPS OpenAI-compatible
   `/chat/completions` base), `AGENT_RUNTIME_MODEL_API_KEY`,
   `AGENT_RUNTIME_MODEL_NAME`, and nonzero integer input/output
   `AGENT_RUNTIME_MODEL_{INPUT,OUTPUT}_MICROUNITS_PER_MILLION` pricing. Credentials
   stay server-side. This feature does not provision a provider or use Dot's login.
3. Enable relay `BUZZ_AGENT_COMMUNITY_ENABLED=true` via the deployment variable
   `MLAI_CHAT_AGENT_COMMUNITY_ENABLED`. Existing pinned native gateway settings
   must be complete. NIP-11 advertises `community-v1` only when enabled.
4. Enable backend `AGENT_RUNTIME_COMMUNITY_ENABLED=true`. Deployment selects the
   `community-agent-worker` Compose service; its profile is opt-in and it runs
   migration checks only. The remote preflight validates host inference readiness
   with `scripts/validate_native_agent_deploy_config.py --provider` before pausing
   writers. The worker is included in all shutdown/rollback writer inventories.
5. Refresh Apps & agents, add Maek · community and explicitly approve its public
   channel consent. Existing personal consent cannot be reused. Consent lasts
   at most 30 days and is tied to the enrolling owner device, like this pilot's
   existing installation lifecycle. It is not a promise of indefinite availability.
6. Verify with synthetic, non-sensitive messages: owner → personal Maek succeeds;
   guest → personal Maek is not offered and forged p-tags are not admitted;
   guest → Maek · community replies once in the same approved thread; another
   channel, revoked caller, paused installation and duplicate trigger fail closed.
   Check provider usage and signed receipts. Do not treat mocked tests as a live
   provider or production verification.

Pause or revoke community mode independently in Apps & agents. For global
rollback, disable the backend community gate first (stops the dedicated worker),
then the relay gate. Personal Dot settings and watch are independent. Keep
registry tombstones so revoked community keys never become ordinary principals.

## Validation

Database-free tests: `agent_runtime.tests.test_community` plus the existing native
and model proxy suites, using `scripts/test_without_database.py`. Deployment
contracts: `tests.test_native_agent_deploy_config`.

Two PostgreSQL regressions in `agent_runtime.tests.test_native_database` exercise
real consent, distinct keys, budget reservation, signed reply deduplication and
caller revocation with synthetic source/model transports. They remain explicitly
gated to the existing reviewed disposable database runner. Running them requires
approval of that runner's exact migration inventory; implementation is not that
approval. No production data, migrations or real inference are needed for unit
checks.
