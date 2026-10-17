"""Native milestones carry actual provider/run proofs, never invented Git SHAs."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from django.test import SimpleTestCase

from . import cms_goal_contract as goals
from .tests_cms_inventory_contract_unit import binding


class CmsGoalContractTests(SimpleTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 17, 5, 0, tzinfo=timezone.utc)
        self.scope = goals.scope(binding(), policy_operation_id=str(uuid4()), policy_digest="a" * 64)
        self.source = {"run_id": "original-run", "run_generation": 2, "envelope_digest": "b" * 64}
        self.goal = goals.new_goal(str(uuid4()), self.scope, now=self.now)
        self.goal = goals.bind_source(self.goal, self.source, expected_scope=self.scope, now=self.now)
        self.proofs = {name: goals.make_proof(name, expected_scope=self.scope, original_source=self.source,
            subject_digest="c" * 64, at=self.now) for name in goals.KINDS}

    def refresh(self, **changes):
        return goals.refresh(self.goal, changes.get("proofs", self.proofs), expected_scope=self.scope, now=changes.get("now", self.now))

    def test_completion_needs_all_native_proofs_and_explicitly_has_no_git_merge(self):
        value = self.refresh()
        self.assertTrue(goals.complete(value, expected_scope=self.scope, now=self.now))
        merged = next(row for row in value["milestones"] if row["name"] == "merged")
        self.assertEqual(merged, {"name": "merged", "required": False, "state": "not_required", "proof": None})
        self.assertNotIn("repository_access", str(value)); self.assertNotIn("source_sha", str(value))
        for name in ("approved", "first_draft", "first_article_live"):
            with self.subTest(name=name):
                partial = self.refresh(proofs={key: proof for key, proof in self.proofs.items() if key != name})
                self.assertFalse(goals.complete(partial, expected_scope=self.scope, now=self.now))

    def test_receipts_booleans_and_rechecksummed_git_or_worker_proof_cannot_complete_a_native_milestone(self):
        for changed in (True, {"approved": True}, {**self.proofs["approved"], "sha": "d" * 40},
                {**self.proofs["approved"], "kind": "backend_approval"}, {**self.proofs["approved"], "source": None}):
            if isinstance(changed, dict): changed["digest"] = goals.digest({k: v for k, v in changed.items() if k != "digest"})
            with self.subTest(changed=type(changed)):
                result = self.refresh(proofs={**self.proofs, "approved": changed})
                self.assertFalse(goals.complete(result, expected_scope=self.scope, now=self.now))

    def test_native_policy_owner_site_generation_and_credential_origins_are_exact_scope(self):
        for key, changed in (("owner_user_id", "99"), ("generation", 3), ("public_origin", "https://foreign.test/"),
                ("api_origin", "https://admin.foreign.test/")):
            selected = deepcopy(self.scope); selected["binding"][key] = changed
            with self.subTest(key=key), self.assertRaises(goals.CmsGoalError): goals.validate_goal(self.goal, selected)
        selected = {**self.scope, "policy_digest": "f" * 64}
        self.assertFalse(goals.valid_proof("approved", self.proofs["approved"], expected_scope=selected, original_source=self.source, now=self.now))

    def test_another_paid_run_or_envelope_and_generation_rollback_cannot_replace_original_source(self):
        for changed in ({"run_id": "other-run"}, {"envelope_digest": "f" * 64}, {"run_generation": 1}, {"run_generation": True}):
            with self.subTest(changed=changed), self.assertRaises(goals.CmsGoalError):
                goals.bind_source(self.goal, {**self.source, **changed}, expected_scope=self.scope, now=self.now)

    def test_real_new_generation_invalidates_only_source_dependent_proofs_and_keeps_history(self):
        complete = self.refresh()
        value = goals.bind_source(complete, {**self.source, "run_generation": 3}, expected_scope=self.scope, now=self.now)
        self.assertFalse(goals.complete(value, expected_scope=self.scope, now=self.now))
        self.assertEqual(len(value["proof_history"]), len(goals.SOURCE_MILESTONES))
        for row in value["milestones"]:
            if row["name"] in goals.SOURCE_MILESTONES:
                self.assertEqual((row["state"], row["proof"]), ("invalidated", None))
            elif row["name"] != "merged":
                self.assertEqual(row["state"], "passed")
        self.assertEqual(goals.bind_source(value, value["source"], expected_scope=self.scope, now=self.now), value)

    def test_provider_ack_or_old_live_body_proof_never_finishes_an_article_goal(self):
        changed = deepcopy(self.proofs)
        changed["first_article_live"]["kind"] = "provider_confirmed"
        changed["first_article_live"]["digest"] = goals.digest({k: v for k, v in changed["first_article_live"].items() if k != "digest"})
        self.assertFalse(goals.complete(self.refresh(proofs=changed), expected_scope=self.scope, now=self.now))
        value = self.refresh()
        self.assertFalse(goals.complete(value, expected_scope=self.scope, now=self.now + timedelta(minutes=16)))
        self.assertTrue(goals.complete(value, expected_scope=self.scope, now=self.now + timedelta(minutes=14)))

    def test_future_proof_and_wrong_boolean_source_types_are_rejected(self):
        for changed in ({"at": (self.now + timedelta(seconds=31)).isoformat()},
                {"source": {**self.source, "run_generation": True}}, {"subject_digest": True}, {"scope_digest": "f" * 64}):
            proof = {**deepcopy(self.proofs["previewed"]), **changed}
            proof["digest"] = goals.digest({k: v for k, v in proof.items() if k != "digest"})
            with self.subTest(changed=changed):
                self.assertFalse(goals.valid_proof("previewed", proof, expected_scope=self.scope, original_source=self.source, now=self.now))

    def test_malformed_native_ledgers_and_invented_merge_requirement_are_not_valid(self):
        for change in ({"schema_version": True}, {"revision": False}, {"kind": "git_goal"}, {"approved": True}):
            with self.assertRaises(goals.CmsGoalError): goals.validate_goal({**deepcopy(self.goal), **change}, self.scope)
        value = deepcopy(self.goal); value["milestones"][5]["required"] = True
        with self.assertRaises(goals.CmsGoalError): goals.validate_goal(value, self.scope)
        for malformed in ("untrusted-proof", {"approved": True}):
            value = deepcopy(self.goal); value["milestones"][0]["proof"] = malformed
            with self.assertRaises(goals.CmsGoalError): goals.validate_goal(value, self.scope)
        value = self.refresh(); value["milestones"][0]["proof"].pop("digest")
        with self.assertRaises(goals.CmsGoalError): goals.validate_goal(value, self.scope)

    def test_source_proof_requires_an_actual_run_and_history_is_bounded_across_refreshes(self):
        with self.assertRaises(goals.CmsGoalError):
            goals.make_proof("first_draft", expected_scope=self.scope, original_source=None, subject_digest="c" * 64, at=self.now)
        value = self.goal
        for index in range(40):
            proofs = {name: {**proof} for name, proof in self.proofs.items()}
            for name in proofs:
                proofs[name] = goals.make_proof(name, expected_scope=self.scope, original_source=self.source,
                    subject_digest=goals.digest(index), at=self.now)
            value = goals.refresh(value, proofs, expected_scope=self.scope, now=self.now)
        self.assertEqual(len(value["proof_history"]), 32)
        self.assertTrue(goals.complete(value, expected_scope=self.scope, now=self.now))
