import hashlib
import inspect
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.assertions import actor_identity_headers, build_actor_assertion
from org_memory.kernel import capture_source_version, revoke_source_access
from org_memory.models import (
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryEvidence,
    MemoryExtractionRun,
    MemoryExtractionStatus,
    MemoryPublication,
    MemoryPublicationStatus,
    MemoryProviderEnablement,
    MemoryPilotDeployment,
    MemoryPilotDeploymentState,
    MemoryScopeStatus,
    MemorySourceScope,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationRole,
    OrganizationRoleAssignment,
    OrganizationSlackWorkspace,
    PublicKnowledgeItem,
    PublicKnowledgeStatus,
    ServicePrincipal,
)
from org_memory.pilot_deployment import approval_allowlist_hashes
from org_memory.service_principals import issue_service_principal_credential


def digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


@override_settings(
    ORG_MEMORY_PUBLICATION_ENABLED=True,
    ORG_MEMORY_PUBLICATION_REQUIRE_SEPARATE_REVIEWER=True,
    ORG_MEMORY_PUBLICATION_BLOCKED_CLASSIFICATIONS=(
        "executive,finance,people_sensitive,no_agent"
    ),
    ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION="test-v1",
    ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="publication-pilot-test-secret-value-123",
)
class PublicKnowledgePublicationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(
            name="Public Brain",
            domain="public-brain.test",
        )
        self.other_organization = Organization.objects.create(
            name="Other Public Brain",
            domain="other-public-brain.test",
        )
        self.proposer = get_user_model().objects.create_user(
            email="publisher@public-brain.test",
        )
        self.reviewer = get_user_model().objects.create_user(
            email="reviewer@public-brain.test",
        )
        OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TPUBLIC1",
        )
        role = OrganizationRole.objects.create(
            organization=self.organization,
            slug="public-knowledge-publisher",
            name="Public knowledge publisher",
        )
        for capability in (
            "view_general_memory",
            "review_claims",
            "publish_knowledge",
        ):
            OrganizationCapabilityGrant.objects.create(
                role=role,
                capability=OrganizationCapability.objects.get(key=capability),
            )
        for user, slack_user_id in (
            (self.proposer, "UPUBLISH1"),
            (self.reviewer, "UREVIEWP1"),
        ):
            OrganizationIdentity.objects.create(
                organization=self.organization,
                user=user,
                provider="slack",
                external_tenant_id="TPUBLIC1",
                external_user_id=slack_user_id,
                email_at_link_time=user.email,
                verified_at=timezone.now(),
            )
            membership = OrganizationMembership.objects.create(
                organization=self.organization,
                user=user,
            )
            OrganizationRoleAssignment.objects.create(
                membership=membership,
                role=role,
            )
        self.admin_principal = ServicePrincipal.objects.create(
            name="admin-publication-test",
            organization=self.organization,
            scopes=["org_memory.read", "org_memory.publish"],
            allowed_surfaces=["admin_roo"],
        )
        self.admin_credential, self.admin_token = (
            issue_service_principal_credential(self.admin_principal)
        )
        allowlist = approval_allowlist_hashes(
            self.organization,
            {
                "pilot_admin_refs": [
                    "slack:UPUBLISH1",
                    "slack:UREVIEWP1",
                ],
                "allowed_slack_contexts": ["channel:GPUBLIC1"],
            },
        )
        MemoryPilotDeployment.objects.create(
            organization=self.organization,
            state=MemoryPilotDeploymentState.ACTIVE,
            approval_manifest_hash="c" * 64,
            approval_review_due_at=timezone.now() + timedelta(days=30),
            allowlist_key_version=allowlist["key_version"],
            actor_ref_hashes=allowlist["actor_hashes"],
            context_ref_hashes=allowlist["context_hashes"],
            approved_provider_count=1,
            approved_source_scope_count=1,
            stage_idempotency_key="publication-api-test-stage",
            activation_idempotency_key="publication-api-test-activate",
            activated_at=timezone.now(),
        )
        self.public_principal = ServicePrincipal.objects.create(
            name="public-roo-publication-test",
            organization=self.organization,
            scopes=["public_knowledge.read"],
            allowed_surfaces=["public_roo"],
        )
        _public_credential, self.public_token = (
            issue_service_principal_credential(self.public_principal)
        )
        self.connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.proposer,
            organization=self.organization,
            external_account_id="publication-linear",
        )
        MemoryProviderEnablement.objects.create(
            organization=self.organization,
            provider="linear",
            is_enabled=True,
            approved_by=self.proposer,
            approved_at=timezone.now(),
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="linear",
            external_connection=self.connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            created_by=self.proposer,
        )
        self.scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="project",
            external_id="public-project",
            name="Public Project",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="committee",
        )
        self.request_number = 0

    def _admin_headers(self, *, reviewer=False, idempotency_key=None):
        self.request_number += 1
        slack_user_id = "UREVIEWP1" if reviewer else "UPUBLISH1"
        request_id = f"publication-{self.request_number}"
        event_id = f"EvPUBLIC{self.request_number}"
        assertion = build_actor_assertion(
            self.admin_token,
            credential_id=str(self.admin_credential.pk),
            surface="admin_roo",
            slack_team_id="TPUBLIC1",
            acting_slack_user_id=slack_user_id,
            slack_channel_id="GPUBLIC1",
            slack_thread_ts="1700000000.123",
            event_id=event_id,
            request_id=request_id,
        )
        identity = actor_identity_headers(
            assertion=assertion,
            surface="admin_roo",
            slack_team_id="TPUBLIC1",
            acting_slack_user_id=slack_user_id,
            slack_channel_id="GPUBLIC1",
            slack_thread_ts="1700000000.123",
            event_id=event_id,
            request_id=request_id,
        )
        headers = {
            "HTTP_AUTHORIZATION": f"ServicePrincipal {self.admin_token}",
            **{
                f"HTTP_{key.upper().replace('-', '_')}": value
                for key, value in identity.items()
            },
        }
        if idempotency_key:
            headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
        return headers

    def _public_headers(self):
        return {
            "HTTP_AUTHORIZATION": f"ServicePrincipal {self.public_token}",
        }

    def _claim(
        self,
        *,
        external_id,
        statement,
        classification="committee",
        version_key="v1",
    ):
        source, version, _created = capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="publication-linear",
            source_type="linear_issue",
            external_id=external_id,
            version_key=version_key,
            content_hash=digest(f"{version_key}:{statement}"),
            classification=classification,
            acl={
                "is_accessible": True,
                "provider_revision": f"acl:{version_key}:{external_id}",
                "principal_refs": ["group:committee"],
            },
            chunks=[
                {
                    "ordinal": 0,
                    "text": statement,
                    "occurred_at": timezone.now(),
                }
            ],
            title=f"Source {external_id}",
            occurred_at=timezone.now(),
            configuration=self.configuration,
            source_scope=self.scope,
        )
        run = MemoryExtractionRun.objects.create(
            organization=self.organization,
            source_version=version,
            idempotency_key=digest(f"publication-run:{external_id}:{version_key}"),
            status=MemoryExtractionStatus.EXTRACTED,
            extractor_version="test-v1",
            schema_version="test-v1",
            prompt_version="test-v1",
            model="deterministic-test",
            prompt_input_hash=digest(statement),
        )
        claim = MemoryClaim.objects.create(
            organization=self.organization,
            extraction_run=run,
            candidate_key=digest(f"candidate:{external_id}:{version_key}"),
            kind=MemoryClaimKind.FACT,
            epistemic_type="observation",
            predicate=f"public_fact_{external_id}",
            object_value=statement,
            statement=statement,
            normalized_key=digest(f"normalized:{external_id}:{version_key}"),
            status=MemoryClaimStatus.ACTIVE,
            classification=classification,
            confidence=Decimal("0.950"),
            importance=Decimal("0.700"),
            source_authority=Decimal("0.900"),
            observed_at=timezone.now(),
            recorded_at=timezone.now(),
            review_required=False,
            extractor_version="test-v1",
            extractor_model="deterministic-test",
            extractor_prompt_version="test-v1",
            extractor_schema_version="test-v1",
        )
        chunk = version.chunks.get()
        MemoryEvidence.objects.create(
            claim=claim,
            source=source,
            source_version=version,
            chunk=chunk,
            quote=statement,
            quote_start=0,
            quote_end=len(statement),
            quote_hash=digest(statement),
            source_locator={"external_id": external_id},
            evidence_confidence=Decimal("1.000"),
        )
        return claim, source

    def _create_and_publish(self, *, claim, public_key, public_body):
        created = self.client.post(
            "/api/v1/org-memory/publications",
            {
                "source_type": "claim",
                "source_id": str(claim.pk),
                "public_key": public_key,
                "public_title": "MLAI community update",
                "public_body": public_body,
                "tags": ["community"],
                "redaction_notes": "Reviewed and removed all private source details.",
                "submit_for_review": True,
                "confirm_redacted": True,
            },
            format="json",
            **self._admin_headers(
                idempotency_key=f"create-{public_key}-{str(claim.pk)}"
            ),
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(
            created.data["status"],
            MemoryPublicationStatus.PENDING_REVIEW,
        )
        resolved = self.client.post(
            f"/api/v1/org-memory/review-items/{created.data['review_id']}/resolve",
            {
                "confirm": True,
                "decision": "approve",
                "reason": "The payload is public-safe and source-backed.",
            },
            format="json",
            **self._admin_headers(
                reviewer=True,
                idempotency_key=f"approve-{public_key}-{str(claim.pk)}",
            ),
        )
        self.assertEqual(resolved.status_code, 200, resolved.data)
        self.assertTrue(resolved.data["created"])
        publication = MemoryPublication.objects.get(pk=created.data["id"])
        return publication, publication.published_item

    def _public_answer(self, query):
        return self.client.post(
            "/api/v1/public-brain/answer",
            {"query": query},
            format="json",
            **self._public_headers(),
        )

    def test_candidate_is_private_until_redacted_and_separately_approved(self):
        claim, _source = self._claim(
            external_id="monthly-meetup",
            statement=(
                "MLAI runs a monthly meetup. Contact secret@public-brain.test "
                "for the private planning notes."
            ),
        )
        created = self.client.post(
            "/api/v1/org-memory/publications",
            {
                "source_type": "claim",
                "source_id": str(claim.pk),
                "public_key": "monthly-community-meetup",
                "redaction_notes": "Initial candidate requires human redaction.",
            },
            format="json",
            **self._admin_headers(
                idempotency_key="create-monthly-community-meetup"
            ),
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["status"], MemoryPublicationStatus.DRAFT)
        self.assertEqual(
            [row["code"] for row in created.data["sensitivity_findings"]],
            ["email_address"],
        )
        before = self._public_answer("monthly meetup")
        self.assertEqual(before.status_code, 200, before.data)
        self.assertEqual(before.data["status"], "abstained")
        self.assertNotIn("secret@", str(before.data))

        edited = self.client.patch(
            f"/api/v1/org-memory/publications/{created.data['id']}",
            {
                "public_title": "Monthly MLAI community meetup",
                "public_body": (
                    "MLAI runs a monthly community meetup in Adelaide."
                ),
                "tags": ["community", "events"],
                "redaction_notes": (
                    "Removed contact details and all private planning references."
                ),
            },
            format="json",
            **self._admin_headers(),
        )
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(edited.data["sensitivity_findings"], [])

        submitted = self.client.post(
            f"/api/v1/org-memory/publications/{created.data['id']}/submit",
            {"confirm_redacted": True},
            format="json",
            **self._admin_headers(),
        )
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.assertEqual(
            submitted.data["status"],
            MemoryPublicationStatus.PENDING_REVIEW,
        )

        self_approval = self.client.post(
            f"/api/v1/org-memory/review-items/{submitted.data['review_id']}/resolve",
            {"confirm": True, "decision": "approve"},
            format="json",
            **self._admin_headers(
                idempotency_key="self-approve-monthly-meetup",
            ),
        )
        self.assertEqual(self_approval.status_code, 400, self_approval.data)
        self.assertIn("different", self_approval.data["detail"])

        approved = self.client.post(
            f"/api/v1/org-memory/review-items/{submitted.data['review_id']}/resolve",
            {
                "confirm": True,
                "decision": "approve",
                "reason": "Independent review completed.",
            },
            format="json",
            **self._admin_headers(
                reviewer=True,
                idempotency_key="reviewer-approve-monthly-meetup",
            ),
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        publication = MemoryPublication.objects.get(pk=created.data["id"])
        self.assertEqual(publication.status, MemoryPublicationStatus.PUBLISHED)
        self.assertNotEqual(publication.proposed_by_id, publication.approved_by_id)
        self.assertEqual(PublicKnowledgeItem.objects.count(), 1)

        after = self._public_answer("monthly community meetup")
        self.assertEqual(after.status_code, 200, after.data)
        self.assertEqual(after.data["status"], "answered")
        self.assertIn("monthly community meetup", after.data["answer"])
        self.assertNotIn("secret@", str(after.data))
        self.assertNotIn(str(claim.pk), str(after.data))

    def test_public_endpoint_never_returns_drafts_rejected_or_other_org_rows(self):
        claim, _source = self._claim(
            external_id="draft-only",
            statement="The internal draft mentions a public innovation workshop.",
        )
        draft = self.client.post(
            "/api/v1/org-memory/publications",
            {
                "source_type": "claim",
                "source_id": str(claim.pk),
                "public_key": "innovation-workshop-draft",
                "public_title": "Innovation workshop",
                "public_body": "MLAI plans a public innovation workshop.",
            },
            format="json",
            **self._admin_headers(
                idempotency_key="create-innovation-workshop-draft"
            ),
        )
        self.assertEqual(draft.status_code, 201, draft.data)
        PublicKnowledgeItem.objects.create(
            organization=self.other_organization,
            public_key="other-secret",
            revision=1,
            title="Other public row",
            body="Other organisation innovation workshop.",
            content_hash=digest("other"),
        )

        response = self._public_answer("innovation workshop")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "abstained")
        self.assertNotIn("internal draft", str(response.data))
        self.assertNotIn("Other organisation", str(response.data))

    def test_source_revocation_automatically_retires_public_item(self):
        claim, source = self._claim(
            external_id="public-event",
            statement="MLAI hosts a public AI builders event.",
        )
        publication, item = self._create_and_publish(
            claim=claim,
            public_key="public-ai-builders-event",
            public_body="MLAI hosts a public AI builders event.",
        )
        self.assertEqual(
            self._public_answer("AI builders event").data["status"],
            "answered",
        )

        revoke_source_access(source, reason="provider_access_removed")

        publication.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(
            publication.status,
            MemoryPublicationStatus.INVALIDATED,
        )
        self.assertEqual(item.status, PublicKnowledgeStatus.REVOKED)
        self.assertEqual(
            self._public_answer("AI builders event").data["status"],
            "abstained",
        )

    def test_new_private_source_version_retires_previous_public_snapshot(self):
        claim, _source = self._claim(
            external_id="event-location",
            statement="The public demo night is in Adelaide.",
        )
        publication, item = self._create_and_publish(
            claim=claim,
            public_key="demo-night-location",
            public_body="The MLAI public demo night is in Adelaide.",
        )

        _source, _version, created = capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="publication-linear",
            source_type="linear_issue",
            external_id="event-location",
            version_key="v2",
            content_hash=digest("v2:The public demo night location is under review."),
            classification="committee",
            acl={
                "is_accessible": True,
                "provider_revision": "acl:v2:event-location",
                "principal_refs": ["group:committee"],
            },
            chunks=[
                {
                    "ordinal": 0,
                    "text": "The public demo night location is under review.",
                    "occurred_at": timezone.now(),
                }
            ],
            title="Source event-location",
            occurred_at=timezone.now(),
            configuration=self.configuration,
            source_scope=self.scope,
        )

        self.assertTrue(created)
        publication.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(
            publication.status,
            MemoryPublicationStatus.INVALIDATED,
        )
        self.assertEqual(item.status, PublicKnowledgeStatus.REVOKED)

    def test_blocked_private_classification_cannot_enter_publication(self):
        finance_claim, _source = self._claim(
            external_id="finance-metric",
            statement="Confidential revenue is 100.",
            classification="finance",
        )
        response = self.client.post(
            "/api/v1/org-memory/publications",
            {
                "source_type": "claim",
                "source_id": str(finance_claim.pk),
                "public_key": "confidential-revenue",
                "public_title": "Revenue",
                "public_body": "Revenue is 100.",
            },
            format="json",
            **self._admin_headers(
                idempotency_key="create-confidential-revenue"
            ),
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(MemoryPublication.objects.exists())
        self.assertFalse(PublicKnowledgeItem.objects.exists())

    def test_new_public_revision_supersedes_old_and_manual_revoke_is_idempotent(self):
        old_claim, _source = self._claim(
            external_id="public-program-old",
            statement="The public program was called Alpha Lab.",
        )
        old_publication, old_item = self._create_and_publish(
            claim=old_claim,
            public_key="public-program",
            public_body="The MLAI public program is called Alpha Lab.",
        )
        new_claim, _source = self._claim(
            external_id="public-program-new",
            statement="The public program is now called Builders Lab.",
        )
        new_publication, new_item = self._create_and_publish(
            claim=new_claim,
            public_key="public-program",
            public_body="The MLAI public program is called Builders Lab.",
        )

        old_item.refresh_from_db()
        self.assertEqual(old_item.status, PublicKnowledgeStatus.SUPERSEDED)
        self.assertEqual(new_item.revision, old_item.revision + 1)
        self.assertEqual(
            PublicKnowledgeItem.objects.filter(
                public_key="public-program",
                status=PublicKnowledgeStatus.ACTIVE,
            ).count(),
            1,
        )
        old_query = self._public_answer("Alpha Lab")
        self.assertEqual(old_query.data["status"], "answered")
        self.assertNotIn("Alpha Lab", old_query.data["answer"])
        self.assertIn("Builders Lab", old_query.data["answer"])
        current = self._public_answer("Builders Lab")
        self.assertEqual(current.data["status"], "answered")
        self.assertEqual(
            current.data["citations"][0]["item_id"],
            str(new_item.pk),
        )

        revoke_url = (
            f"/api/v1/org-memory/publications/{new_publication.pk}/revoke"
        )
        revoked = self.client.post(
            revoke_url,
            {"confirm": True, "reason": "Public program is being renamed again."},
            format="json",
            **self._admin_headers(
                reviewer=True,
                idempotency_key="revoke-public-program-revision",
            ),
        )
        replay = self.client.post(
            revoke_url,
            {"confirm": True, "reason": "Public program is being renamed again."},
            format="json",
            **self._admin_headers(
                reviewer=True,
                idempotency_key="revoke-public-program-revision",
            ),
        )

        self.assertEqual(revoked.status_code, 200, revoked.data)
        self.assertTrue(revoked.data["created"])
        self.assertFalse(replay.data["created"])
        new_item.refresh_from_db()
        new_publication.refresh_from_db()
        self.assertEqual(new_item.status, PublicKnowledgeStatus.REVOKED)
        self.assertEqual(
            new_publication.status,
            MemoryPublicationStatus.REVOKED,
        )
        self.assertEqual(
            self._public_answer("Builders Lab").data["status"],
            "abstained",
        )
        old_publication.refresh_from_db()
        self.assertEqual(
            old_publication.status,
            MemoryPublicationStatus.PUBLISHED,
        )

    def test_public_surface_cannot_call_private_publication_api(self):
        response = self.client.get(
            "/api/v1/org-memory/publications",
            **self._public_headers(),
        )

        self.assertEqual(response.status_code, 401)

    def test_disabled_feature_flag_blocks_review_approval(self):
        claim, _source = self._claim(
            external_id="disabled-publication",
            statement="MLAI hosts a public founder workshop.",
        )
        created = self.client.post(
            "/api/v1/org-memory/publications",
            {
                "source_type": "claim",
                "source_id": str(claim.pk),
                "public_key": "public-founder-workshop",
                "public_title": "Public founder workshop",
                "public_body": "MLAI hosts a public founder workshop.",
                "redaction_notes": "Reviewed and removed all private source details.",
                "submit_for_review": True,
                "confirm_redacted": True,
            },
            format="json",
            **self._admin_headers(
                idempotency_key="create-disabled-publication"
            ),
        )
        self.assertEqual(created.status_code, 201, created.data)

        with override_settings(ORG_MEMORY_PUBLICATION_ENABLED=False):
            response = self.client.post(
                f"/api/v1/org-memory/review-items/{created.data['review_id']}/resolve",
                {"confirm": True, "decision": "approve"},
                format="json",
                **self._admin_headers(
                    reviewer=True,
                    idempotency_key="approve-disabled-publication",
                ),
            )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertFalse(PublicKnowledgeItem.objects.exists())

    def test_public_answer_modules_do_not_import_private_retrieval(self):
        from org_memory import public_knowledge, public_views

        source = inspect.getsource(public_knowledge) + inspect.getsource(public_views)
        self.assertNotIn("from .retrieval", source)
        self.assertNotIn("from .search", source)
        self.assertNotIn("from .answering", source)
        PublicKnowledgeItem.objects.create(
            organization=self.organization,
            public_key="query-boundary",
            revision=1,
            title="Public query boundary",
            body="This public row proves the isolated query boundary.",
            content_hash=digest("query-boundary"),
        )

        with CaptureQueriesContext(connection) as captured:
            response = self._public_answer("isolated query boundary")

        self.assertEqual(response.status_code, 200, response.data)
        sql = "\n".join(query["sql"].casefold() for query in captured.captured_queries)
        for private_table in (
            "org_memory_memoryclaim",
            "org_memory_memoryevidence",
            "org_memory_memorysource",
            "org_memory_memorychunk",
            "org_memory_memorysummary",
            "org_memory_memorypublication",
        ):
            self.assertNotIn(private_table, sql)
        self.assertIn("org_memory_publicknowledgeitem", sql)
