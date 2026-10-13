# Articles setup merge policy — Phase A11

Setup merges use one policy from the preparation worker or an explicit owner
action. A status or bootstrap GET only observes a pull request; it never merges
one. Existing website consent, rollout restrictions and preview proof remain
required, and every provider merge includes the validated PR head SHA.

The preparation worker respects `requires_review`. GitHub branch rules and
available classic protection metadata can require an owner/reviewer action.
Pending checks, rate limits and temporary provider failures remain scheduled
work. A protection lookup returning 403 or 404 is unknown policy, so the merge
endpoint enforces GitHub's actual restrictions. A negative merge response never
becomes a successful merge receipt.

## Verified permission mismatch

GitHub documents `Administration: read` for [Get branch protection](https://docs.github.com/en/rest/branches/branch-protection#get-branch-protection).
The repository token in `integrations/services/github_app.py` requests only
contents and pull-request permissions. [Get rules for a branch](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch)
needs only Metadata read. This confirms the code/permission mismatch in the
runbook; it does not establish that every production lookup failed. Production
tokens were not inspected or minted for this verification.

## Regression evidence

Python 3.11, database-free runner, actual consent, token, policy and merge chain;
only provider/credential, cache and database boundaries are replaced.

```text
.venv/bin/python /tmp/articles-a11-baseline/scripts/test_without_database.py content_factory.tests_setup_merge_policy_unit
```

The baseline is a detached worktree at `32438da0` containing the same regression
module. The baseline reproduces the protection permission stop, check waits and
the status GET merge. The PR records the exact before/after output.

```text
.venv/bin/python scripts/test_without_database.py content_factory.tests_setup_merge_policy_unit
Ran 15 tests
OK
```

Database-backed tests, live customer merges and production rollout are outside
this validation. Rollback is a code revert; there is no schema change.
