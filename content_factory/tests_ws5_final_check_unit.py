"""Workstream 5: a readable, self-resolving final check (backend side).

La Vie's article was held "for staff" four times and nobody could see why
without a server session on Content Factory; its author bio carried phrases
("leads our editorial team", "over a decade of research") her site never
stated. Synthetic records only; run with scripts/test_without_database.py.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase, mock

from django.test import override_settings

from .article_quality_record import MAX_FIELD_CHARS, MAX_FINDINGS, quality_record
from .author_bio_check import about_page_text, author_bio_warnings, unsupported_bio_phrases
from .hosted_quality_state import founder_quality
from .management.commands.article_quality import render
from .staff_console import _quality_findings
from . import staff_hold_alerts

CLAIM = "Gisele has led our editorial team for over a decade."
FINDING = {"code": "claim_insufficient", "severity": "blocking", "claim": CLAIM,
           "reason": "Not stated on the About page.", "source": "authored"}


def held(**overrides):
    return {"status": "blocking_findings", "self_heal_exhausted": True, "escalation": "quality_review",
            "hold_owner": "staff", "self_heal_fingerprint": "f" * 64, "self_heal_attempt": 2,
            "self_heal_stop_reason": "attempt_limit",
            "self_heal_history": [{"attempt": 1, "buckets": ["claim_removal"], "targets": ["claim_removal:abc"],
                                   "result": "preview_queued", "fingerprint": "x"}], **overrides}


def run_with(quality, **overrides):
    values = {"run_id": "run-1", "pk": 7, "workflow": "article_generation", "organization_id": 3,
              "organization": SimpleNamespace(pk=3), "run_request": {}, "result": {"article_preview_quality": quality}}
    return SimpleNamespace(**{**values, **overrides})


class QualityRecordTests(TestCase):
    def test_record_carries_the_findings_history_and_hold_owner(self):
        record = quality_record(run_with(held(findings_detail=[FINDING])))
        self.assertEqual(record["holdOwner"], "staff")
        self.assertEqual(record["findings"], [FINDING])
        self.assertEqual(record["history"], [{"attempt": 1, "buckets": ["claim_removal"],
                                              "targets": ["claim_removal:abc"], "result": "preview_queued"}])
        lines = render(record)
        self.assertIn("Run run-1: status blocking_findings, held for staff.", lines)
        self.assertIn(f"   Claim: {CLAIM}", lines)

    def test_record_recaps_items_and_strips_urls_with_query_strings(self):
        noisy = {**FINDING, "reason": "See https://lavie.example/about?utm=1 " + "x" * 900}
        record = quality_record(run_with(held(findings_detail=[noisy] * 80)))
        self.assertEqual(len(record["findings"]), MAX_FINDINGS)
        self.assertNotIn("utm=", str(record))
        self.assertTrue(all(len(item["reason"]) <= MAX_FIELD_CHARS for item in record["findings"]))

    def test_staff_console_row_is_unchanged_without_a_findings_list(self):
        self.assertEqual(_quality_findings(run_with(held())), {})
        row = _quality_findings(run_with(held(findings_detail=[FINDING])))
        self.assertEqual(row["qualityFindings"], [FINDING])
        self.assertEqual(row["qualityHoldOwner"], "staff")

    def test_command_says_when_no_findings_were_sent(self):
        lines = render(quality_record(run_with(held())))
        self.assertTrue(any("No findings list was sent" in line for line in lines))


class FounderRemovedClaimsTests(TestCase):
    def test_founder_sees_removed_sentences_but_never_the_findings(self):
        quality = {"status": "passed", "findings_detail": [FINDING],
                   "self_heal_removed_claims": [{"attempt": 1, "removed": [CLAIM]}]}
        view = founder_quality(quality)
        self.assertEqual(view["removed_claims"], [CLAIM])
        self.assertNotIn("findings_detail", view)
        self.assertNotIn("removed_claims", founder_quality({"status": "passed"}))


class StaffHoldAlertTests(TestCase):
    def test_nothing_is_queued_with_the_setting_off(self):
        with override_settings(ARTICLES_STAFF_HOLD_ALERTS_ENABLED=False), \
                mock.patch("content_factory.founder_journey_alerts.queue_founder_journey_alert") as queue:
            self.assertIsNone(staff_hold_alerts.queue_staff_hold_alert(run_with(held(findings_detail=[FINDING]))))
            self.assertEqual(staff_hold_alerts.run_staff_hold_alert_sweep(), {"status": "disabled"})
        queue.assert_not_called()

    def test_a_staff_hold_alerts_once_with_the_top_three_finding_codes(self):
        findings = [{**FINDING, "code": f"code_{index}", "severity": "advisory" if index == 0 else "blocking"}
                    for index in range(5)]
        run = run_with(held(findings_detail=findings))
        with override_settings(ARTICLES_STAFF_HOLD_ALERTS_ENABLED=True), \
                mock.patch("content_factory.founder_journey_alerts.is_scheduler_run", return_value=False), \
                mock.patch("content_factory.founder_journey_alerts.queue_founder_journey_alert",
                           return_value="alert-1") as queue:
            self.assertEqual(staff_hold_alerts.queue_staff_hold_alert(run), "alert-1")
        queue.assert_called_once()
        kwargs = queue.call_args.kwargs
        self.assertEqual(kwargs["alert_reason"], "article_staff_hold")
        self.assertEqual(kwargs["details"], {"finding_1": "code_1", "finding_2": "code_2", "finding_3": "code_3",
                                             "stop_reason": "attempt_limit"})
        self.assertTrue(kwargs["slot"].startswith("hold-"))
        self.assertNotIn(CLAIM, str(kwargs["details"]))

    def test_a_founder_hold_is_not_a_staff_alert(self):
        run = run_with(held(hold_owner="founder"))
        with override_settings(ARTICLES_STAFF_HOLD_ALERTS_ENABLED=True), \
                mock.patch("content_factory.founder_journey_alerts.is_scheduler_run", return_value=False), \
                mock.patch("content_factory.founder_journey_alerts.queue_founder_journey_alert") as queue:
            self.assertIsNone(staff_hold_alerts.queue_staff_hold_alert(run))
        queue.assert_not_called()

    def test_the_hold_alerts_again_after_24_hours(self):
        now = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
        receipt = {"alert_id": "alert-1", "queued_at": (now - timedelta(hours=25)).isoformat()}
        run = run_with(held())
        run.result["ops_alerts"] = {"article_staff_hold": receipt}
        with override_settings(ARTICLES_STAFF_HOLD_ALERTS_ENABLED=True), \
                mock.patch("content_factory.founder_journey_alerts.is_scheduler_run", return_value=False), \
                mock.patch("content_factory.founder_journey_alerts.queue_founder_journey_alert",
                           side_effect=["alert-1", "alert-2"]) as queue:
            self.assertEqual(staff_hold_alerts.queue_staff_hold_alert(run, now=now), "alert-2")
        self.assertEqual([call.kwargs["alert_reason"] for call in queue.call_args_list],
                         ["article_staff_hold", "article_staff_hold_24h"])

    def test_no_reminder_before_24_hours(self):
        now = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
        run = run_with(held())
        run.result["ops_alerts"] = {"article_staff_hold": {"alert_id": "alert-1",
                                                           "queued_at": (now - timedelta(hours=2)).isoformat()}}
        with override_settings(ARTICLES_STAFF_HOLD_ALERTS_ENABLED=True), \
                mock.patch("content_factory.founder_journey_alerts.is_scheduler_run", return_value=False), \
                mock.patch("content_factory.founder_journey_alerts.queue_founder_journey_alert",
                           return_value="alert-1") as queue:
            staff_hold_alerts.queue_staff_hold_alert(run, now=now)
        self.assertEqual(queue.call_count, 1)


ABOUT = ("<html><body><nav>Home</nav><h1>About La Vie</h1><p>Gisele Benkemoun is an architect and the founder "
         "of La Vie. She designs calm, light-filled homes in Sydney.</p><script>var x='decade';</script></body></html>")


class AuthorBioCheckTests(TestCase):
    def test_the_la_vie_bio_phrases_the_site_never_stated_are_returned(self):
        bio = ("Gisele Benkemoun is an architect and the founder of La Vie. She leads our editorial team. "
               "She brings over a decade of research into sustainable homes.")
        phrases = unsupported_bio_phrases(bio, "Gisele Benkemoun is an architect and the founder of La Vie. "
                                               "She designs calm, light-filled homes in Sydney.")
        self.assertEqual(phrases, ["She leads our editorial team.",
                                   "She brings over a decade of research into sustainable homes."])

    def test_script_text_on_the_page_never_supports_a_bio(self):
        text = about_page_text("lavie.example", fetch=lambda url, domain: (ABOUT.encode(), {}))
        self.assertNotIn("decade", text)
        self.assertIn("founder of La Vie", text)

    def test_warning_only_while_the_setting_is_on(self):
        authors = [{"id": "gisele", "bio": "She leads our editorial team."}]
        fetch = mock.Mock(return_value=(ABOUT.encode(), {}))
        with override_settings(ARTICLES_AUTHOR_BIO_CHECK_ENABLED=False):
            self.assertIsNone(author_bio_warnings(authors, "lavie.example", fetch=fetch))
        fetch.assert_not_called()
        with override_settings(ARTICLES_AUTHOR_BIO_CHECK_ENABLED=True):
            warning = author_bio_warnings(authors, "lavie.example", fetch=fetch)
        self.assertEqual(warning["status"], "unsupported")
        self.assertEqual(warning["items"], [{"authorId": "gisele", "phrases": ["She leads our editorial team."]}])

    def test_an_unreadable_about_page_is_unchecked_not_an_error(self):
        def missing(url, domain):
            raise OSError("no route")
        with override_settings(ARTICLES_AUTHOR_BIO_CHECK_ENABLED=True):
            warning = author_bio_warnings([{"id": "a", "bio": "Writes about homes."}], "lavie.example", fetch=missing)
        self.assertEqual(warning["status"], "unchecked")
