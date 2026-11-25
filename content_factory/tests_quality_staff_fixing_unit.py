"""An exhausted quality hold on an MLAI-generated artefact is staff_fixing (plan A20).

La Vie pk 5396 (18 Nov 2026): the preview check held the article on "Article
image is not bound to this run's generated manifest", our own check on our own
output, and told the founder the preview needed a technical check. Synthetic
records only; run with scripts/test_without_database.py.
"""
from unittest import TestCase

from django.test import override_settings

from .hosted_quality_state import (
    PREVIEW_INPUTS_MESSAGE, STAFF_FIXING_MESSAGE, UNALERTED_FALLBACK_MESSAGE, founder_result, hold_owner,
    preview_quality_publish_notice, staff_fixing,
)

URL = "https://preview.example/draft"
IMAGE_BINDING = {"code": "visible_review_inputs_invalid",
                 "errors": ["Visible review inputs are incomplete: Article image is not bound to this run's generated manifest"]}


def held(**overrides):
    return {"status": "blocking_findings", "self_heal_exhausted": True, "escalation": "quality_review",
            "preview_url": URL, "resume_generation": 1, **overrides}


def result_with(quality):
    return {"live_preview": {"previewUrl": URL, "resumeGeneration": 1}, "article_preview_quality": quality,
            "approval_blocker": {"code": "article_preview_editorial_review_blocked", "message": "staff diagnostic",
                                 "founder_message": "Some statements or links still need an evidence check."}}


class HoldOwnerTests(TestCase):
    def test_content_factory_names_the_owner(self):
        self.assertEqual(hold_owner(held(hold_owner="staff")), "staff")
        self.assertEqual(hold_owner(held(hold_owner="founder")), "founder")

    def test_records_from_before_the_owner_was_named(self):
        # La Vie 5396: our image binding check failed as invalid review inputs.
        self.assertEqual(hold_owner(held(preview_inputs_invalid=True, visible_content_acceptance=IMAGE_BINDING)), "staff")
        self.assertEqual(hold_owner(held(visible_content_acceptance=IMAGE_BINDING)), "staff")
        # A finding about the founder's claims or offer stays a founder hold.
        claims = {"code": "visible_review_rejected", "errors": ["claim-001: unsupported"]}
        self.assertEqual(hold_owner(held(visible_content_acceptance=claims)), "founder")

    def test_safety_and_unfinished_checks_are_never_staff_fixing(self):
        self.assertEqual(hold_owner(held(escalation="safety_review", hold_owner="staff", preview_inputs_invalid=True)),
                         "founder")
        self.assertIsNone(hold_owner(held(self_heal_exhausted=False, hold_owner="staff")))
        self.assertIsNone(hold_owner({**held(hold_owner="staff"), "status": "self_healing"}))
        self.assertIsNone(hold_owner(None))


class StaffFixingPresentationTests(TestCase):
    @override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True)
    def test_founder_is_told_nothing_is_needed_not_a_technical_check(self):
        quality = held(hold_owner="staff", preview_inputs_invalid=True, visible_content_acceptance=IMAGE_BINDING,
                       founder_message=STAFF_FIXING_MESSAGE)
        view = founder_result(result_with(quality))
        projected = view["article_preview_quality"]
        self.assertEqual(projected["founder_message"], STAFF_FIXING_MESSAGE)
        self.assertEqual(projected["hold_owner"], "staff")
        self.assertNotIn("preview_inputs_invalid", projected)
        self.assertEqual(view["approval_blocker"]["founder_message"], STAFF_FIXING_MESSAGE)
        self.assertEqual(view["approval_blocker"]["owner"], "staff")
        self.assertNotIn("message", view["approval_blocker"])
        self.assertEqual(preview_quality_publish_notice(quality), ("blocked", STAFF_FIXING_MESSAGE))
        self.assertTrue(staff_fixing(quality))

    @override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False)
    def test_copy_never_claims_a_fix_no_alert_would_start(self):
        quality = held(hold_owner="staff", founder_message=STAFF_FIXING_MESSAGE)
        self.assertEqual(founder_result(result_with(quality))["article_preview_quality"]["founder_message"],
                         UNALERTED_FALLBACK_MESSAGE)
        self.assertEqual(preview_quality_publish_notice(quality), ("blocked", UNALERTED_FALLBACK_MESSAGE))

    @override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True)
    def test_founder_content_finding_is_still_a_founder_hold(self):
        quality = held(hold_owner="founder", founder_message="Some statements still need an evidence check.")
        view = founder_result(result_with(quality))
        self.assertNotEqual(view["article_preview_quality"]["founder_message"], STAFF_FIXING_MESSAGE)
        self.assertNotEqual(view["approval_blocker"]["founder_message"], STAFF_FIXING_MESSAGE)
        self.assertFalse(staff_fixing(quality))
        legacy = held(preview_inputs_invalid=True, hold_owner="founder")
        self.assertEqual(preview_quality_publish_notice(legacy), ("blocked", PREVIEW_INPUTS_MESSAGE))


class StaffFixingAlertTests(TestCase):
    def test_the_staff_alert_rule_fires_for_a_staff_fixing_hold(self):
        from types import SimpleNamespace
        from django.utils import timezone
        from .staff_alert_rules import _run_reasons
        run = SimpleNamespace(result={"article_preview_quality": held(hold_owner="staff")}, run_request={},
                              status="approval_required", created_at=timezone.now(), workflow="article_generation")
        self.assertIn("article_quality_review_required", _run_reasons(run, timezone.now()))


class HonestRetryStatusTests(TestCase):
    """Founder-proof plan WS6: a staff-owned hold never offers the founder a recheck."""

    def projected(self, quality):
        return founder_result(result_with(quality))["article_preview_quality"]

    def test_flag_off_keeps_todays_payload(self):
        with override_settings(ARTICLES_HONEST_RETRY_STATUS_ENABLED=False):
            self.assertNotIn("retryable", self.projected(held(hold_owner="staff")))

    @override_settings(ARTICLES_HONEST_RETRY_STATUS_ENABLED=True)
    def test_a_staff_hold_is_not_retryable_for_the_founder(self):
        projected = self.projected(held(hold_owner="staff"))
        self.assertEqual((projected["hold_owner"], projected["retryable"]), ("staff", False))

    @override_settings(ARTICLES_HONEST_RETRY_STATUS_ENABLED=True)
    def test_a_founder_hold_keeps_its_recheck(self):
        claims = {"code": "visible_review_rejected", "errors": ["claim-001: unsupported"]}
        self.assertNotIn("retryable", self.projected(held(hold_owner="founder", visible_content_acceptance=claims)))
