"""Immutable publication integration; run only with exact migration approval."""
import copy
from datetime import date
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase

from community_chat.startups.presentation import update_payload
from organizations.models import Organization
from startup_updates.evidence_contract import content_hash
from startup_updates.models import MonthlyEvidenceSnapshot, MonthlyUpdateDraft
from startup_updates.revisions import approve_and_publish, RevisionConflict, revision_payload, save_revision
from startup_updates.tests_financial_history_unit import evidence_payload, replace_totals


class FinancialHistoryPublicationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Synthetic financial history", domain="history.example.invalid")
        self.actor = get_user_model().objects.create_user(email="history@example.invalid", password="synthetic-test-password")
        self.month = date(2026, 8, 1)
        self.draft = MonthlyUpdateDraft.objects.create(organization=self.organization, month=self.month)
        self.snapshot = self.make_snapshot(evidence_payload())

    def make_snapshot(self, payload):
        return MonthlyEvidenceSnapshot.objects.create(organization=self.organization, month=self.month,
            payload=payload, content_hash=content_hash(payload))

    def save(self, *, selection=None, snapshot=None, text="We shipped the release."):
        self.draft.refresh_from_db()
        memo = {"summary": text}
        if selection is not None:
            memo["_financial_history_selection"] = selection
        return save_revision(self.draft, memo, snapshot=snapshot or self.snapshot, audience="community",
            expected_revision=self.draft.current_revision_id,
            validation={"groundedness_status": "founder_asserted"})

    def publish(self, revision):
        return approve_and_publish(self.draft, actor=self.actor, revision_id=revision.pk,
            revision_hash=revision.content_hash, audience_visibility=["community"])

    def published(self):
        self.draft.refresh_from_db()
        return update_payload(self.draft, published=True, community=True)

    def test_published_history_and_approval_stay_frozen_across_new_source_evidence_and_text_edits(self):
        first = self.save(selection={"enabled": True, "shareAmounts": False})
        self.publish(first)
        before = copy.deepcopy(self.published())
        payload = evidence_payload()
        replace_totals(payload, -1, 9000, 300)
        new_snapshot = self.make_snapshot(payload)
        edited = self.save(snapshot=new_snapshot, text="We shipped another release.")
        self.assertEqual(edited.structured_memo["financial_history"], first.structured_memo["financial_history"])
        self.assertEqual(self.published(), before)
        self.assertEqual(first.approval.content_hash, first.content_hash)
        first.refresh_from_db()
        self.assertEqual(first.structured_memo["financial_history"]["snapshot"]["points"][-1]["revenue"], 600)
        self.assertEqual(first.structured_memo["financial_history"]["_source"], {
            "snapshotId": self.snapshot.pk, "snapshotHash": self.snapshot.content_hash})
        receipt = revision_payload(edited)
        self.assertEqual(receipt["snapshotId"], new_snapshot.pk)
        self.assertEqual(receipt["evidenceSnapshot"]["charts"]["performance"][-1]["income"], 9000)
        proof = receipt["evidenceSnapshot"]["financial_history_evidence"]
        self.assertEqual(proof["snapshotId"], self.snapshot.pk)
        self.assertEqual(proof["snapshotHash"], self.snapshot.content_hash)
        self.assertEqual(proof["charts"]["performance"][-1]["income"], 600)
        self.assertNotIn("financial_history_evidence", new_snapshot.payload)
        self.assertIsNone(revision_payload(edited, include_evidence=False)["evidenceSnapshot"])
        self.assertNotIn("_source", str(self.published()))
        self.assertNotIn("snapshotHash", str(self.published()["financialHistory"]))

    def test_opt_in_and_refresh_each_need_a_new_exact_revision_approval(self):
        first = self.save(selection={"enabled": True, "shareAmounts": False})
        self.publish(first)
        amounts = self.save(selection={"enabled": True, "shareAmounts": True})
        self.assertNotEqual(first.content_hash, amounts.content_hash)
        self.assertFalse(self.published()["financialHistory"]["shareAmounts"])
        with self.assertRaises(RevisionConflict):
            self.publish(first)
        self.publish(amounts)
        self.assertEqual(self.published()["financialHistory"]["points"][-1]["revenue"], 600)
        payload = evidence_payload()
        replace_totals(payload, -1, 9000, 300)
        refreshed = self.save(snapshot=self.make_snapshot(payload),
            selection={"enabled": True, "shareAmounts": True, "refresh": True})
        self.assertEqual(self.published()["financialHistory"]["points"][-1]["revenue"], 600)
        self.publish(refreshed)
        self.assertEqual(self.published()["financialHistory"]["points"][-1]["revenue"], 9000)
        proof = revision_payload(refreshed)["evidenceSnapshot"]["financial_history_evidence"]
        self.assertEqual(proof["snapshotId"], refreshed.snapshot_id)
        self.assertEqual(proof["charts"]["performance"][-1]["income"], 9000)
        self.assertNotEqual(first.structured_memo["financial_history"]["_source"],
            refreshed.structured_memo["financial_history"]["_source"])

    def test_history_proof_rejects_missing_tampered_or_cross_scope_sources(self):
        revision = self.save(selection={"enabled": True, "shareAmounts": False})
        other = Organization.objects.create(name="Other synthetic startup", domain="other.example.invalid")
        other_company = MonthlyEvidenceSnapshot.objects.create(organization=other, month=self.month,
            payload=self.snapshot.payload, content_hash=self.snapshot.content_hash)
        other_month_payload = evidence_payload()
        other_month_payload["period"]["month"] = "2026-07-01"
        other_month = MonthlyEvidenceSnapshot.objects.create(organization=self.organization, month=date(2026, 7, 1),
            payload=other_month_payload, content_hash=content_hash(other_month_payload))
        tampered_payload = evidence_payload()
        replace_totals(tampered_payload, -1, 999999, 300)
        tampered = MonthlyEvidenceSnapshot.objects.create(organization=self.organization, month=self.month,
            payload=tampered_payload, content_hash="0" * 64)
        bad_sources = [None, {}, {"snapshotId": self.snapshot.pk, "snapshotHash": "wrong"}]
        bad_sources += [{"snapshotId": source.pk, "snapshotHash": source.content_hash}
            for source in (other_company, other_month, tampered)]
        for source in bad_sources:
            with self.subTest(source=source):
                memo = copy.deepcopy(revision.structured_memo)
                if source is None:
                    memo["financial_history"].pop("_source")
                else:
                    memo["financial_history"]["_source"] = source
                candidate = SimpleNamespace(pk=revision.pk, number=revision.number,
                    content_hash=revision.content_hash, snapshot_id=revision.snapshot_id,
                    snapshot=revision.snapshot, audience=revision.audience, structured_memo=memo)
                self.assertIsNone(revision_payload(candidate)["evidenceSnapshot"]["financial_history_evidence"])

    def test_captured_snapshot_hash_encoding_resolves_original_history_proof(self):
        payload = evidence_payload()
        payload["hash"] = content_hash(payload)
        MonthlyEvidenceSnapshot.objects.filter(pk=self.snapshot.pk).delete()
        snapshot = MonthlyEvidenceSnapshot.objects.create(organization=self.organization, month=self.month,
            payload=payload, content_hash=payload["hash"])
        revision = self.save(snapshot=snapshot, selection={"enabled": True, "shareAmounts": True})
        proof = revision_payload(revision)["evidenceSnapshot"]["financial_history_evidence"]
        self.assertEqual(proof["snapshotHash"], payload["hash"])
        self.assertEqual(proof["charts"], payload["charts"])

    def test_legacy_posts_remain_without_history_until_reviewed_opt_in(self):
        first = self.save()
        self.publish(first)
        before = copy.deepcopy(self.published())
        self.assertIsNone(before["financialHistory"])
        self.assertNotIn("financial_history", first.structured_memo)
        self.save(text="An ordinary writing edit.")
        self.assertEqual(self.published(), before)
        opted_in = self.save(selection={"enabled": True, "shareAmounts": False})
        self.assertEqual(self.published(), before)
        self.publish(opted_in)
        self.assertIsNotNone(self.published()["financialHistory"])

    def test_disabled_history_stays_disabled_through_redrafting(self):
        first = self.save(selection={"enabled": True, "shareAmounts": True})
        self.publish(first)
        off = self.save(selection={"enabled": False, "shareAmounts": False})
        self.publish(off)
        redrafted = self.save(text="A new founder-reviewed story.")
        self.assertFalse(redrafted.structured_memo["financial_history"]["enabled"])
        self.publish(redrafted)
        self.assertIsNone(self.published()["financialHistory"])
