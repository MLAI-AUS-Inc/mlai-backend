import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from . import cybertest_personal as personal
from .cybertest_checkout import _payer, _sponsored, create_quote, recover_quote
from .cybertest_entry import EntryAuthority, EntryError
from .cybertest_policy import CheckoutError
from .cybertest_personal_scope import workspace_id
from .test_cybertest_entry import Cache


class PersonalReviewTests(SimpleTestCase):
    def test_personal_codes_and_grants_cannot_be_exchanged_with_company_credentials(self):
        cache = Cache()
        workspace = workspace_id(uuid4())
        identity = dict(
            user_id="19", company_id=workspace, workspace_id=workspace, company_name="My reviews"
        )
        company = EntryAuthority(cache, lambda *args: identity)
        member = EntryAuthority(cache, lambda *args: identity, namespace="personal-basic-v1")
        state, verifier = "x" * 43, "y" * 43
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        for owner, outsider in [(member, company), (company, member)]:
            code = owner.issue("session", "19", workspace, state, challenge)
            with self.assertRaises(EntryError):
                outsider.exchange(code, state, verifier)
            grant = owner.exchange(code, state, verifier)["grant"]
            with self.assertRaises(EntryError):
                outsider.inspect(grant)
            self.assertEqual(owner.inspect(grant)["workspace_id"], workspace)

    @override_settings(CYBERTEST_PHASED_SCANS_ENABLED=True)
    def test_personal_workspace_uses_only_authenticated_member_and_checks_membership(self):
        user = SimpleNamespace(community_chat_profile_id=uuid4())
        session = SimpleNamespace(user=user, user_id=19)
        with (
            patch.object(personal, "local_origin"),
            patch.object(personal, "_valid_session", return_value=True),
            patch.object(personal, "_validate_device_owner"),
            patch.object(personal, "require_community_access") as access,
            patch.object(personal.CommunityChatAccountSession.objects, "select_related") as query,
        ):
            query.return_value.filter.return_value.first.return_value = session
            workspace = workspace_id(user.community_chat_profile_id)
            self.assertEqual(
                personal.personal_identity("session", workspace)["workspace_id"], workspace
            )
            access.assert_called_with(user)
            with self.assertRaises(EntryError):
                personal.personal_identity("session", workspace_id(uuid4()))

    @override_settings(CYBERTEST_FREE_REPOSITORY_IDS=["10"])
    def test_only_members_own_wallet_can_authorize_personal_quote(self):
        user = SimpleNamespace(pk=19, is_active=True, community_chat_profile_id=uuid4())
        workspace = workspace_id(user.community_chat_profile_id)
        binding = dict(
            mlai_company_id=workspace,
            workspace_id=workspace,
            mlai_user_id="19",
            repository_id="10",
            purchase={"phase": "basic"},
        )
        self.assertFalse(_sponsored(binding))
        row = SimpleNamespace(binding=binding, user_id=None)
        _payer(row, user)
        with self.assertRaises(CheckoutError):
            _payer(row, SimpleNamespace(pk=20, is_active=True, community_chat_profile_id=uuid4()))
        user.community_chat_profile_id = uuid4()
        with self.assertRaises(CheckoutError):
            _payer(row, user)

    def test_personal_checkout_requires_a_source_plan_and_rejects_upgrade_recovery(self):
        workspace = workspace_id(uuid4())
        intent = dict(
            github_actor_id="100",
            repository_id="10",
            repository="member/test",
            workspace_id=workspace,
            scope="all",
            mlai_company_id=workspace,
            mlai_user_id="19",
        )
        token = "t" * 43
        with patch("roo.cybertest_checkout.configured_mode", return_value="test"):
            with self.assertRaises(CheckoutError) as rejected:
                create_quote.__wrapped__(str(uuid4()), token, intent)
            self.assertEqual(rejected.exception.code, "BASIC_REQUIRED")
            with self.assertRaises(CheckoutError) as rejected:
                recover_quote.__wrapped__(intent, token, parent_checkout_id=str(uuid4()))
            self.assertEqual(rejected.exception.code, "BASIC_REQUIRED")

    def test_private_service_routes_deny_unauthenticated_callers(self):
        for operation in ("exchange", "inspect"):
            response = APIClient().post(
                f"/api/v1/points/cybertest/entry/personal/{operation}/", {}, format="json"
            )
            self.assertEqual(response.status_code, 403)
