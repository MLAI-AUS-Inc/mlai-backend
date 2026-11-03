"""Permissioned mapping writes and atomic refresh audits on synthetic contacts."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from crm.audience_profiles import AUDIENCE_PROFILE_KEY, AudienceProfileError
from crm.models import Contact, CrmAuditEvent

from .api_fixtures import CrmApiMixin, PREFIX, crm_enabled


@crm_enabled
class AudienceProfileDatabaseTests(CrmApiMixin, TestCase):
    def mapping(self):
        return {"id": "reviewed-job", "source_kind": "csv", "source_key": "attributes.job",
                "facet": "career", "provenance": "imported", "reviewed": True, "aliases": {}}

    def request_body(self):
        return {"mappings": [self.mapping()], "records": [{"mapping_id": "reviewed-job",
                "source_record_id": "synthetic-import-row", "observed_at": "2026-01-01T00:00:00Z", "value": "Developer"}]}

    def test_mapping_preview_then_apply_is_attributed_idempotent_and_preserves_consent(self):
        contact = Contact.objects.create(email="mapping@example.test", attributes={"marketing_review_hold": True},
                                         marketing_consent=Contact.MarketingConsent.WITHDRAWN)
        root = PREFIX + f"contacts/{contact.pk}/audience-profile/"
        body = self.request_body()
        denied = self.member_client.post(root + "import/", body, format="json")
        self.assertEqual(denied.status_code, 403)
        preview = self.client.post(root + "preview/", body, format="json")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertFalse(preview.json()["applied"])
        contact.refresh_from_db()
        self.assertNotIn(AUDIENCE_PROFILE_KEY, contact.attributes)
        for _ in range(2):
            applied = self.client.post(root + "import/", body, format="json")
            self.assertEqual(applied.status_code, 200, applied.content)
            self.assertTrue(applied.json()["applied"])
        contact.refresh_from_db()
        facet = contact.attributes[AUDIENCE_PROFILE_KEY]["facets"]["career"]
        self.assertEqual((facet["values"], facet["source_kind"], facet["provenance"]), (["developer"], "csv", "imported"))
        self.assertEqual(facet["observed_at"], "2026-01-01T00:00:00.000000Z")
        self.assertEqual(contact.marketing_consent, Contact.MarketingConsent.WITHDRAWN)
        self.assertTrue(contact.attributes["marketing_review_hold"])
        audits = CrmAuditEvent.objects.filter(target_type="contact", target_id=str(contact.pk))
        self.assertEqual(audits.count(), 1)
        self.assertEqual(audits.get().detail, {"operation": "audience_source_mapping", "fields": ["career"]})

    def test_missing_contact_profile_writes_return_404(self):
        root = PREFIX + f"contacts/{uuid.uuid4()}/audience-profile/"
        for suffix, body in (("import/", self.request_body()), ("", {})):
            response = self.client.post(root + suffix, body, format="json")
            self.assertEqual(response.status_code, 404, response.content)

    def test_refresh_audits_each_committed_contact_before_later_record_failure(self):
        first = Contact.objects.create(id=uuid.UUID(int=1), email="first@example.test", attributes={"job": "Developer"})
        second = Contact.objects.create(id=uuid.UUID(int=2), email="second@example.test", attributes={"job": {"malformed": True}})
        with TemporaryDirectory(prefix="synthetic-audience-mapping-") as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"mappings": [self.mapping()]}))
            with self.assertRaises(AudienceProfileError):
                call_command("crm_refresh_audience_profiles", mapping_file=str(manifest),
                             observed_at="2026-01-01T00:00:00Z", apply=True, actor_id=self.admin.pk)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIn(AUDIENCE_PROFILE_KEY, first.attributes)
        self.assertNotIn(AUDIENCE_PROFILE_KEY, second.attributes)
        audit = CrmAuditEvent.objects.get(target_type="contact", target_id=str(first.pk))
        self.assertEqual(audit.detail, {"operation": "audience_profile_refresh", "mapping_ids": ["reviewed-job"], "fields": ["career"]})
