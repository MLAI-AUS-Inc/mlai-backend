import json
from datetime import timedelta
from io import StringIO

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from integrations.fields import CredentialEncryptionError, decrypt_credential_value
from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.assertions import actor_identity_headers, build_actor_assertion
from org_memory.models import (
    ActorAssertionReceipt,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationRole,
    OrganizationRoleAssignment,
    OrganizationSlackIdentity,
    OrganizationSlackWorkspace,
    ServicePrincipal,
    ServicePrincipalAuditEvent,
)
from org_memory.service_principals import (
    authenticate_service_principal_token,
    issue_service_principal_credential,
    parse_service_principal_token,
    revoke_service_principal_credential,
)
from roo.models import PointsAdmin
from startup_updates.models import UserStartupBinding


CONTRACT_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"
CONTRACT_ASSERTION = (
    "eyJhY3Rpbmdfc2xhY2tfdXNlcl9pZCI6IlVBRE1JTjEyMyIsImV2ZW50X2lkIjoiRXYwMVRFU1QiLCJleHAiOjE3MDAwMDAwNDUsImlhdCI6MTcwMDAwMDAwMCwia2lkIjoiYWFhYWFhYWEtYWFhYS1hYWFhLWFhYWEtYWFhYWFhYWFhYWFhIiwibm9uY2UiOiJmaXhlZF9ub25jZV8xMjM0NTY3ODkwMTIzNDUiLCJyZXF1ZXN0X2lkIjoicm9vLXRlc3QtcmVxdWVzdCIsInNsYWNrX2NoYW5uZWxfaWQiOiJHQURNSU4xMjMiLCJzbGFja190ZWFtX2lkIjoiVE1MQUkxMjMiLCJzbGFja190aHJlYWRfdHMiOiIxNzAwMDAwMDAwLjEyMyIsInN1cmZhY2UiOiJhZG1pbl9yb28iLCJ2IjoxfQ."
    "l71Zpd8GgCU4I7CCa-1x2yzeeVbdvIfmePqQDDu-iuk"
)


class ActorAssertionContractVectorTests(SimpleTestCase):
    def test_backend_signer_matches_the_roo_contract_vector(self):
        assertion = build_actor_assertion(
            CONTRACT_TOKEN,
            credential_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            surface="admin_roo",
            slack_team_id="TMLAI123",
            acting_slack_user_id="UADMIN123",
            slack_channel_id="GADMIN123",
            slack_thread_ts="1700000000.123",
            event_id="Ev01TEST",
            request_id="roo-test-request",
            issued_at=1_700_000_000,
            nonce="fixed_nonce_123456789012345",
        )

        self.assertEqual(assertion, CONTRACT_ASSERTION)


class ServicePrincipalLifecycleTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.test")
        self.principal = ServicePrincipal.objects.create(
            name="roo-admin-test",
            organization=self.organization,
            scopes=["org_memory.read"],
            allowed_surfaces=["admin_roo"],
        )

    def test_plaintext_token_is_never_persisted_and_revocation_is_audited(self):
        credential, token = issue_service_principal_credential(self.principal)

        credential.refresh_from_db()
        self.assertNotIn(token, credential.secret_hash)
        self.assertNotIn(token, credential.token_hint)
        self.assertEqual(authenticate_service_principal_token(token).principal, self.principal)

        revoke_service_principal_credential(credential, reason="test")
        with self.assertRaisesMessage(ValueError, "Inactive"):
            authenticate_service_principal_token(token)
        self.assertTrue(
            ServicePrincipalAuditEvent.objects.filter(
                principal=self.principal,
                event_type="credential_revoked",
            ).exists()
        )

    def test_rotation_can_expire_the_old_credential_without_changing_identity(self):
        old_credential, old_token = issue_service_principal_credential(self.principal)
        new_credential, new_token = issue_service_principal_credential(
            self.principal,
            rotated_from=old_credential,
        )
        old_credential.expires_at = timezone.now() - timedelta(seconds=1)
        old_credential.save(update_fields=("expires_at",))

        with self.assertRaisesMessage(ValueError, "Inactive"):
            authenticate_service_principal_token(old_token)
        self.assertEqual(authenticate_service_principal_token(new_token).credential, new_credential)


class OrgMemoryActorBoundaryTests(TestCase):
    endpoint = "/api/v1/org-memory/auth/context"

    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.test")
        self.other_organization = Organization.objects.create(name="Other", domain="other.test")
        self.user = get_user_model().objects.create_user(
            email="admin@mlai.test",
            slack_id="UADMIN123",
        )
        self.workspace = OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TMLAI123",
            name="MLAI Test",
        )
        OrganizationSlackIdentity.objects.create(
            workspace=self.workspace,
            slack_user_id="UADMIN123",
            user=self.user,
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.user,
            provider="slack",
            external_tenant_id="TMLAI123",
            external_user_id="UADMIN123",
            email_at_link_time=self.user.email,
            verified_at=timezone.now(),
        )
        self.membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        role = OrganizationRole.objects.create(
            organization=self.organization,
            slug="admin-roo-reader",
            name="Admin Roo reader",
        )
        OrganizationRoleAssignment.objects.create(
            membership=self.membership,
            role=role,
        )
        OrganizationCapabilityGrant.objects.create(
            role=role,
            capability=OrganizationCapability.objects.get(key="view_general_memory"),
        )
        self.principal = ServicePrincipal.objects.create(
            name="roo-admin-test",
            organization=self.organization,
            scopes=["org_memory.read"],
            allowed_surfaces=["admin_roo"],
        )
        self.credential, self.token = issue_service_principal_credential(self.principal)

    def _headers(
        self,
        *,
        token=None,
        credential=None,
        surface="admin_roo",
        team_id="TMLAI123",
        user_id="UADMIN123",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
        event_id="Ev01TEST",
        request_id="roo-test-request",
        nonce=None,
        issued_at=None,
        ttl_seconds=45,
    ):
        token = token or self.token
        credential = credential or self.credential
        assertion = build_actor_assertion(
            token,
            credential_id=str(credential.pk),
            surface=surface,
            slack_team_id=team_id,
            acting_slack_user_id=user_id,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            event_id=event_id,
            request_id=request_id,
            nonce=nonce,
            issued_at=issued_at,
            ttl_seconds=ttl_seconds,
        )
        identity = actor_identity_headers(
            assertion=assertion,
            surface=surface,
            slack_team_id=team_id,
            acting_slack_user_id=user_id,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            event_id=event_id,
            request_id=request_id,
        )
        return {
            "HTTP_AUTHORIZATION": f"ServicePrincipal {token}",
            **{f"HTTP_{key.upper().replace('-', '_')}": value for key, value in identity.items()},
        }

    def test_valid_assertion_resolves_trusted_organisation_and_actor(self):
        response = self.client.get(self.endpoint, **self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["organization_id"], self.organization.pk)
        self.assertEqual(response.data["acting_slack_user_id"], "UADMIN123")
        self.assertEqual(response.data["user_id"], self.user.pk)
        self.assertEqual(response.data["membership_id"], self.membership.pk)
        self.assertTrue(response.data["memory_class_access"]["general"])
        self.assertFalse(response.data["memory_class_access"]["finance"])
        self.assertFalse(response.data["memory_class_access"]["no_agent"])
        self.assertEqual(ActorAssertionReceipt.objects.count(), 1)

    def test_assertion_is_single_use_across_requests(self):
        headers = self._headers(nonce="fixed_replay_nonce_123456789")

        self.assertEqual(self.client.get(self.endpoint, **headers).status_code, 200)
        replay = self.client.get(self.endpoint, **headers)

        self.assertEqual(replay.status_code, 401)
        self.assertIn("already been used", str(replay.data))

    def test_raw_actor_header_cannot_be_changed_after_signing(self):
        headers = self._headers()
        headers["HTTP_X_ACTING_SLACK_USER_ID"] = "UFORGED123"

        response = self.client.get(self.endpoint, **headers)

        self.assertEqual(response.status_code, 401)
        self.assertIn("does not match", str(response.data))

    @override_settings(
        ROO_API_KEY="valid-public-roo-legacy-key",
        INTERNAL_API_KEY="valid-public-roo-legacy-key",
    )
    def test_public_surface_and_legacy_public_key_cannot_enter_private_memory(self):
        public_principal = ServicePrincipal.objects.create(
            name="roo-public-test",
            organization=self.organization,
            scopes=["org_memory.read"],
            allowed_surfaces=["public_roo"],
        )
        public_credential, public_token = issue_service_principal_credential(public_principal)

        public_response = self.client.get(
            self.endpoint,
            **self._headers(
                token=public_token,
                credential=public_credential,
                surface="public_roo",
                request_id="roo-public-forgery",
            ),
        )
        legacy_response = self.client.get(
            self.endpoint,
            HTTP_X_API_KEY="valid-public-roo-legacy-key",
            HTTP_X_ACTING_SLACK_USER_ID="UADMIN123",
        )

        self.assertEqual(public_response.status_code, 401)
        self.assertEqual(legacy_response.status_code, 401)

    def test_principal_cannot_cross_to_another_workspace_organisation(self):
        other_workspace = OrganizationSlackWorkspace.objects.create(
            organization=self.other_organization,
            slack_team_id="TOTHER123",
        )
        OrganizationSlackIdentity.objects.create(
            workspace=other_workspace,
            slack_user_id="UADMIN123",
            user=self.user,
        )

        response = self.client.get(
            self.endpoint,
            **self._headers(team_id="TOTHER123", request_id="cross-org-attempt"),
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn("cross organisation", str(response.data))

    def test_actor_mapping_cannot_cross_to_another_workspace_in_the_same_organisation(self):
        OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TMLAI999",
        )

        response = self.client.get(
            self.endpoint,
            **self._headers(team_id="TMLAI999", request_id="cross-workspace-attempt"),
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn("verifiably mapped", str(response.data))

    def test_unmapped_actor_and_expired_assertion_are_denied(self):
        unmapped = self.client.get(
            self.endpoint,
            **self._headers(user_id="UUNKNOWN123", request_id="unmapped-actor"),
        )
        expired = self.client.get(
            self.endpoint,
            **self._headers(
                request_id="expired-assertion",
                issued_at=int(timezone.now().timestamp()) - 120,
                ttl_seconds=45,
            ),
        )

        self.assertEqual(unmapped.status_code, 401)
        self.assertEqual(expired.status_code, 401)

    def test_missing_scope_is_forbidden_after_identity_verification(self):
        self.principal.scopes = []
        self.principal.save(update_fields=("scopes",))

        response = self.client.get(self.endpoint, **self._headers())

        self.assertEqual(response.status_code, 403)

    def test_legacy_roles_and_forged_capability_headers_cannot_replace_membership(self):
        PointsAdmin.objects.create(
            slack_user_id="UADMIN123",
            user=self.user,
            role="admin",
        )
        UserStartupBinding.objects.create(
            organization=self.organization,
            user=self.user,
            role="admin",
        )
        self.membership.delete()
        headers = self._headers(request_id="forged-role-capability")
        headers["HTTP_X_ORGANIZATION_ROLE"] = "admin"
        headers["HTTP_X_ORGANIZATION_CAPABILITY"] = "view_general_memory"

        response = self.client.get(self.endpoint, **headers)

        self.assertEqual(response.status_code, 403)


class ConnectorCredentialEncryptionTests(TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode("ascii")
        self.override = override_settings(
            CONNECTOR_CREDENTIAL_KEYS=json.dumps({"v7": self.key}),
            CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="v7",
            IS_LOCAL_ENV=False,
        )
        self.override.enable()
        self.user = get_user_model().objects.create_user(email="founder@example.test")
        self.organization = Organization.objects.create(name="Encrypted", domain="encrypted.test")

    def tearDown(self):
        self.override.disable()

    def test_connector_secret_round_trips_and_raw_database_value_is_versioned(self):
        connection_row = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.organization,
            access_token="super-secret-token",
        )

        connection_row.refresh_from_db()
        self.assertEqual(connection_row.access_token, "super-secret-token")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT access_token FROM integrations_externalserviceconnection WHERE id = %s",
                [connection_row.pk],
            )
            raw_value = cursor.fetchone()[0]
        self.assertTrue(raw_value.startswith("mlai-enc:v1:v7:"))
        self.assertNotIn("super-secret-token", raw_value)

    def test_missing_or_wrong_key_fails_closed(self):
        with override_settings(
            CONNECTOR_CREDENTIAL_KEYS=json.dumps({"v8": Fernet.generate_key().decode("ascii")}),
            CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="v8",
        ):
            with self.assertRaises(CredentialEncryptionError):
                decrypt_credential_value(
                    "mlai-enc:v1:v7:gAAAAABinvalid"
                )
        with override_settings(
            CONNECTOR_CREDENTIAL_KEYS="",
            CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="",
            IS_LOCAL_ENV=False,
        ):
            with self.assertRaises(ImproperlyConfigured):
                ExternalServiceConnection.objects.create(
                    provider="linear",
                    user=self.user,
                    organization=self.organization,
                    access_token="must-not-write-plaintext",
                )

    def test_rotation_command_reencrypts_with_the_new_active_key(self):
        connection_row = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.organization,
            access_token="rotate-me",
        )
        replacement_key = Fernet.generate_key().decode("ascii")

        with override_settings(
            CONNECTOR_CREDENTIAL_KEYS=json.dumps(
                {"v7": self.key, "v8": replacement_key}
            ),
            CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="v8",
        ):
            call_command("rotate_connector_credentials", stdout=StringIO())
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT access_token FROM integrations_externalserviceconnection WHERE id = %s",
                    [connection_row.pk],
                )
                raw_value = cursor.fetchone()[0]
            self.assertTrue(raw_value.startswith("mlai-enc:v1:v8:"))
            connection_row.refresh_from_db()
            self.assertEqual(connection_row.access_token, "rotate-me")
