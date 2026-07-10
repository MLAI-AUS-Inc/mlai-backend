"""
Contract test against contracts/run-statuses.json.

The status vocabulary crossing the mlai<->content-factory seam lives in ONE
schema artifact, vendored byte-identically in both repos. This test pins this
repo's enums and wizard classification sets to that artifact:

- a status added/removed here without updating the contract fails this test;
- a status added on the content-factory side arrives via the synced contract
  file and fails this test until it is classified here (historically one new
  cf status cost five PRs across two repos to chase down).

content-factory's mirror test lives at backend/tests/test_status_contract.py
in the content-factory repo.
"""
import json
from pathlib import Path

from django.test import SimpleTestCase

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "run-statuses.json"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


class RunStatusContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.contract = _contract()

    def test_contract_file_exists_and_is_versioned(self):
        self.assertGreaterEqual(int(self.contract["_meta"]["version"]), 1)

    def test_run_status_enum_matches_contract(self):
        from workflow_runs.models import ContentFactoryRunStatus

        expected = set(self.contract["run_statuses"]) | set(
            self.contract["run_status_platform_extensions"]
        )
        self.assertEqual(set(ContentFactoryRunStatus.values), expected)

    def test_step_status_enum_matches_contract(self):
        from workflow_runs.models import ContentFactoryStepStatus

        self.assertEqual(set(ContentFactoryStepStatus.values), set(self.contract["step_statuses"]))

    def test_approval_state_enum_matches_contract(self):
        from workflow_runs.models import ContentFactoryApprovalState

        self.assertEqual(
            set(ContentFactoryApprovalState.values), set(self.contract["approval_states"])
        )

    def test_external_labels_cover_every_run_status_and_normalize_back(self):
        from content_factory.vibe_marketing_views import _normalize_remote_run_status
        from workflow_runs.models import ContentFactoryRunStatus

        labels = self.contract["run_status_external_labels"]
        self.assertEqual(set(labels.keys()), set(self.contract["run_statuses"]))
        # Every label content-factory's GET /api/runs can return must fold
        # back into a canonical local status (not fall through to a guess).
        allowed = set(ContentFactoryRunStatus.values)
        for canonical, label in labels.items():
            normalized = _normalize_remote_run_status(label)
            self.assertIn(
                normalized,
                allowed,
                f"external label {label!r} does not normalize into the run enum",
            )
            # A label that is itself a valid local status (e.g.
            # approval_required, a platform extension) may normalize to
            # itself; everything else must fold back to its canonical status.
            expected_targets = {canonical}
            if label in allowed:
                expected_targets.add(label)
            self.assertIn(
                normalized,
                expected_targets,
                f"external label {label!r} should normalize to one of {sorted(expected_targets)}",
            )


class SetupStatusContractTests(SimpleTestCase):
    """The wizard-ownership classification must cover the factory vocabulary."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.contract = _contract()
        cls.factory = set(cls.contract["setup_statuses_factory"])
        cls.platform = set(cls.contract["setup_statuses_platform"])

    def test_factory_and_platform_vocabularies_are_disjoint(self):
        self.assertEqual(self.factory & self.platform, set())

    def test_every_factory_status_is_classified(self):
        # THE seam guard: when content-factory adds a setup status (and the
        # synced contract file gains it), this fails until the wizard
        # explicitly decides whether that status holds ownership.
        from content_factory.vibe_marketing_views import (
            ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES,
            ARTICLE_SYSTEM_SETUP_NONBLOCKING_STATUSES,
        )

        classified = (
            ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES | ARTICLE_SYSTEM_SETUP_NONBLOCKING_STATUSES
        )
        unclassified = self.factory - classified
        self.assertEqual(
            unclassified,
            set(),
            "content-factory can emit setup statuses the wizard does not "
            f"classify as blocking or non-blocking: {sorted(unclassified)}. "
            "Add each to ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES or "
            "ARTICLE_SYSTEM_SETUP_NONBLOCKING_STATUSES in "
            "content_factory/vibe_marketing_views.py.",
        )

    def test_classification_sets_contain_no_unknown_statuses(self):
        from content_factory.vibe_marketing_views import (
            ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES,
            ARTICLE_SYSTEM_SETUP_MERGED_STATUSES,
            ARTICLE_SYSTEM_SETUP_NONBLOCKING_STATUSES,
            ARTICLE_SYSTEM_SETUP_REVIEWABLE_STATUSES,
        )

        vocabulary = self.factory | self.platform
        for name, status_set in (
            ("ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES", ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES),
            ("ARTICLE_SYSTEM_SETUP_NONBLOCKING_STATUSES", ARTICLE_SYSTEM_SETUP_NONBLOCKING_STATUSES),
            ("ARTICLE_SYSTEM_SETUP_REVIEWABLE_STATUSES", ARTICLE_SYSTEM_SETUP_REVIEWABLE_STATUSES),
            ("ARTICLE_SYSTEM_SETUP_MERGED_STATUSES", ARTICLE_SYSTEM_SETUP_MERGED_STATUSES),
        ):
            unknown = set(status_set) - vocabulary
            self.assertEqual(
                unknown,
                set(),
                f"{name} contains statuses missing from contracts/run-statuses.json: "
                f"{sorted(unknown)}. Add them to the contract (both repos, in lockstep) "
                "or remove them here.",
            )

    def test_blocking_and_nonblocking_are_disjoint(self):
        from content_factory.vibe_marketing_views import (
            ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES,
            ARTICLE_SYSTEM_SETUP_NONBLOCKING_STATUSES,
        )

        self.assertEqual(
            ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES & ARTICLE_SYSTEM_SETUP_NONBLOCKING_STATUSES,
            set(),
        )

    def test_reviewable_statuses_all_block(self):
        from content_factory.vibe_marketing_views import (
            ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES,
            ARTICLE_SYSTEM_SETUP_REVIEWABLE_STATUSES,
        )

        self.assertEqual(
            ARTICLE_SYSTEM_SETUP_REVIEWABLE_STATUSES - ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES,
            set(),
        )
