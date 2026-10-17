# E5 / v2 3.3: upfront native CMS publishing choices

The setup owner can now save separate CMS publication choices through `cms-configure`: review the final article or publish automatically, explicit publication consent and confirmation of automatic native defaults. The controller verifies the current founder/company/connection/credential before each read, performs no provider HTTP under database locks, and fences the result before saving. A duplicate request that completes during native discovery reuses the same current acknowledgement. No post, user, category or tag is created by configuration.

WordPress uses the authenticated native author with verified edit/publish capabilities and reads an actual existing category in deterministic ID order, including empty categories. It never assumes category ID `1`. Ghost reads an actual active native author; malformed/empty/unsupported metadata fails instead of inventing identities. Tags are explicitly empty. Only bounded labels and identifiers are retained, with no private staff email or arbitrary provider response. The owner response projects the saved choices and current connection version, without API origins or credential references.

The current native profile retains generated images inside the reviewed article body. WordPress explicitly sets featured media to zero; Ghost explicitly sets the feature image to null. This avoids adding a second theme hero to already reviewed inline content. The Ghost post journal now accepts and verifies that optional native field while retaining old two-field intents for existing history. A separate featured-hero layout would require a pre-review native render profile; it cannot be introduced as a publication-time rewrite.

## Private consent and original generation

The policy lives in a closed, current private `cms-policy` operation bound to the exact site, owner, credential reference and verified native template. Neither a public projection, worker Boolean nor the existing Git auto-publish preference can establish this consent. Its private ID and digest are sealed into the original CMS artifact reservation before generation. Only that saved native policy can select `auto` in the original envelope; existing requests without it retain manual review. Changing policy or selecting a foreign/stale reference invalidates the old scope.

The later article-publication guard resolves only the policy reserved before its original paid generation. A separately configured policy cannot retroactively turn an older manual draft into an auto-approved publication. Worker callbacks still cannot send approval, native publication proofs or provider credentials. Private policy operations are excluded from general owner operation detail, summaries, the Git journey and cleanup scheduling, and purge erases them across generations.

## Validation and remaining work

The no-database website group passes 1,047 checks, including 14 new policy/native-discovery/owner-route cases and the Ghost native-field recovery case. Tests cover explicit consent, current owner/scope, before-generation sealing, stale-policy rejection, idempotent discovery races, actual non-default category IDs and the difference from Git auto approval. Model checks and migration dry-run report no changes; the failure audit checks 775 emissions and all 556 test modules are assigned.

The disposable remote-CI CMS flow additionally saves and resolves actual native policy rows before reserving the original auto artifact and checks purge. That ORM execution is remote-only. All local provider/model/wallet boundaries are synthetic; no actual CMS configuration request, publishing, upload, account operation, merge or deployment ran. All rollout flags remain off.

Next, record a separate exact staged-article approval from this native policy (or the owner when review was chosen), then wire it to the durable post journal, live article evidence and per-site first-article goal. The setup UI must collect these choices before dispatching generation, so they require no second setup action. CMS consent alone is not a publication receipt.
