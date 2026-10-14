# D1: continue a site goal through the first live article

14 October 2026. Continuous implementation authorisation applies. D1 is active. The first billing/refund slice is implemented locally; the
Ready-to-live runner is still required. ARTICLES_FIRST_ARTICLE_GOAL_ENABLED remains false. No migration
or real charge, generation, merge, deployment or customer operation is authorised
by this note.

Use the existing owned start_article_generation operation and existing article
approval/publish/merge machinery. Add first_draft and first_article_live goal actions
and collect their real scoped evidence. Ready is an intermediate milestone for a
first_article_live target. Automatic publication is allowed only by the saved C1
consent, still subject to mandatory organisation review. Review-first produces a
single review_draft action; the existing guarded owner approval resumes the goal.
No request JSON field, model output or legacy org auto_publish flag can manufacture
that consent. The article approval receipt must bind the exact goal, draft and
source before publish; A11/C4 still decide merge authority.

Pick the highest-opportunity uncovered editorial candidate at the current source,
filtered through the persisted customer/audience/catalogue decisions. Persist the
selection and request identity before dispatch. If an inventory candidate becomes
covered, choose the next eligible candidate without asking the owner to repeat topic
selection. Use the article pipeline's research/gates rather than charging an extra
standalone research request outside the initial consent.

Billing must honour the reserved initial quote. C1 currently seals the quote but
has not debited a hold. Reserve the first-article points at goal acceptance in the
same transaction, using the existing PointsService/ledger and a deterministic goal
request key; do not add a new hold model. This prevents another purchase consuming
the balance during a long setup. Generation consumes that original payer/price and
ledger rather than re-pricing or charging a second time. The normal HTTP/MCP charge
path retains its existing quote checks. Any internal goal-reservation argument is
server-only and must re-load the goal and original SPEND ledger; body fields confer
no authority. Free reservations are server-recorded and scoped, never inferred from
missing ledger data. Existing PointsService validates spend replay by payer/kind/
source/amount/reference, so a later topic description does not change the charge.

Defer ordinary child terminal-failure refunds while a valid active first-article
goal is repairing that same paid request. Refund the original ledger once on final
technical failure, or cancellation before useful draft work is delivered; preserve
its payer and purchased-point allocation. No new spend key after an uncertain
dispatch, callback loss or worker restart. A source-only accepted B6 transition
preserves billing and consent. A changed connection/site must not carry authority.

Own article merges need a trusted source transition: do not restart the entire
setup when this goal's article PR moves the default branch. Re-verify the exact
merged source through the appropriate hosted/native proof and fetch the real public
article with its required marker. Store first_article_live_receipt on the parent;
its exact digest is the final article_deployment_marker proof. C5 then validates
that receipt's source, domain and URL before sending the live-article notification.
No done state from a draft, preview URL, opened PR, merge alone or missing receipt.

Validation must execute the actual ready-to-live chain with database/provider
boundaries faked, not replace the planner, dispatcher, billing policy or approval
checks. Cover automatic vs review-first, mandatory review, stale/foreign source,
no duplicate charges, refund lineage, lost dispatch/callback, own merge transition,
late live marker, cancellation and final C5 notice URL. Model drift checks must
remain empty; all rollouts stay off.

Future E5 channels must provide their own source and live-publication evidence;
CMS/MLAI-hosted origins cannot borrow a GitHub/native proof or bypass URL ownership.

Implementation checkpoint: the owner start transaction reserves one existing Roo
SPEND using a fixed first-article request key. Canonical generation has a private
server argument that reloads this held payment, rechecks original owner/site,
current operation and exact ledger, preserves the original price/payer and still
runs normal provider admission and the durable dispatch outbox. HTTP body fields
cannot select this path. Source-only consent rebinding keeps the same payment.

Child terminal-refund paths defer credit while the original paid goal is active.
Final stop/cancel/supersede saves a goal-refund outbox row in the same winning
transaction. Credit runs independently of rollout/enrollment or the connection's
current generation, validates its original spend, and reuses the existing refund
key/allocation. Revocation never depends on a credit succeeding. Losing a prepare
CAS cannot queue a credit. Account erasure retains only scoped financial proof
and parent references until settlement, then removes the revoked website shell;
customer run/draft fields are removed from that retained parent.

Validation: 21 focused billing/refund tests; all 19 database-free workflow commands
passed, plus model-drift check (no changes), source credential scan, production
failure-emitter audit (661), and CI assignment (353 full, zero new omissions).
Database/provider boundaries are faked. No real wallet or generation was used.

Before a D1 PR or enabling this flag: finish topic/customer-profile selection,
Ready-to-live dispatch, actual article gates/approval/merge/live receipts and the
full-chain tests. Also cover switching choices while the previous goal's credit
is pending: new setup must wait automatically for that original credit instead
of rolling back withdrawal of old publication consent or requiring a repeat click.
This slice is a checkpoint, not a complete or releasable D1 implementation.
