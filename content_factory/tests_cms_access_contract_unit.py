"""Pending owner access metadata cannot invent another grant, source or lifetime."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from . import cms_access_contract as contract
from .channel_artifact_contract import digest
from .tests_cms_inventory_contract_unit import binding


class CmsAccessContractTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.fields = {"operation_id": str(uuid4()), "root_operation_id": str(uuid4()), "root_action": "cms-goal",
            "binding": binding(), "configuration_revision": 2, "credential_operation_id": str(uuid4()), "credential_digest": "a" * 64,
            "principal_id": "7", "request_id": str(uuid4()), "channel_choice_digest": "b" * 64, "access_epoch": "c" * 64,
            "request_digest": "d" * 64, "proposal_digest": "e" * 64}
        self.value = contract.make_intent(**self.fields, now=self.now)

    def test_original_metadata_is_closed_bounded_and_contains_no_credential_or_fake_goal(self):
        self.assertEqual(contract.validate_intent(self.value), self.value)
        self.assertEqual(self.value["expires_at"], (self.now + timedelta(days=1)).isoformat())
        self.assertEqual(self.value["root_operation_id"], self.fields["root_operation_id"])
        for forbidden in ("secret", "sealed_credential", "goal", "policy", "milestones", "public_url", "billing_request_id"):
            self.assertNotIn(forbidden, self.value)
        self.assertEqual(contract.make_intent(**{**self.fields, "root_action": "cms-setup"}, now=self.now)["root_action"], "cms-setup")

    def test_rechecksumming_fields_or_extending_expiry_does_not_change_the_original_shape(self):
        for changes in ({"schema_version": True}, {"kind": "approved"}, {"secret": "private-input"},
                {"expires_at": (self.now + timedelta(days=2)).isoformat()}, {"configuration_revision": True},
                {"operation_id": self.fields["root_operation_id"]}, {"principal_id": True}, {"root_action": "prepare"}):
            candidate = {**deepcopy(self.value), **changes}
            candidate["digest"] = digest({key: item for key, item in candidate.items() if key != "digest"})
            with self.subTest(changes=changes), self.assertRaises(contract.CmsAccessContractError): contract.validate_intent(candidate)

    def test_foreign_or_changed_references_are_consistency_only_not_current_authority(self):
        changed = {**self.fields, "root_operation_id": str(uuid4()), "request_id": str(uuid4())}
        other = contract.make_intent(**changed, now=self.now)
        self.assertNotEqual(other["digest"], self.value["digest"])
        # Validation only parses declared metadata. The worker must reload the
        # actual owning private root/key before this can authorize any GET.
        self.assertEqual(contract.validate_intent(other)["root_operation_id"], changed["root_operation_id"])

    def test_platform_principal_types_and_resource_ids_are_exact(self):
        for changes in ({"credential_operation_id": "untrusted"}, {"request_id": True}, {"configuration_revision": -1},
                {"principal_id": "0"}, {"credential_digest": "untrusted"}):
            with self.subTest(changes=changes), self.assertRaises(contract.CmsAccessContractError):
                contract.make_intent(**{**self.fields, **changes}, now=self.now)
        ghost = deepcopy(self.fields); ghost["binding"]["platform"] = "ghost"; ghost["principal_id"] = "a" * 24
        self.assertEqual(contract.make_intent(**ghost, now=self.now)["principal_id"], "a" * 24)
