"""Plan C15: CMS-held content stops honestly at repository selection; never create a database.

The scans under ``testdata/cms-content`` were recorded from Content Factory's
own fixture repositories (``backend/tests/fixtures/cms-content``) through its
inventory scan. A WordPress theme and a Next site whose blog is fed by
Contentful end in ``cannot_yet:cms_content`` before any setup run, charge or
staff alert; a Markdown blog beside a CMS keeps going.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import tests_one_click_operations_unit as operation_fixtures
from . import website_cms_content as cms_content
from . import website_prepare as prepare
from .website_discovery import discovery_snapshot

SCANS = Path(__file__).parent / "testdata" / "cms-content"


def recorded(name):
    return json.loads((SCANS / f"{name}.scan.json").read_text())


def discovery(name):
    scan = recorded(name)
    snapshot = discovery_snapshot(scan)
    snapshot["sourceSha"] = scan["repo_head_sha"]
    return snapshot


class DiscoveryTierTests(SimpleTestCase):
    def test_recorded_cms_scans_project_cannot_yet_cms_content(self):
        for name, cms in (("wordpress-theme", "wordpress"), ("next-contentful-blog", "headless_cms")):
            with self.subTest(name=name):
                value = discovery(name)
                self.assertEqual(value["contentSource"]["cms"], cms)
                self.assertEqual(value["setupTier"]["id"], "cannot_yet")
                self.assertEqual(value["setupTier"]["reason"], "cms_content")
                self.assertEqual(value["supportLevel"], "cannot_yet")

    def test_a_markdown_blog_beside_a_cms_is_a_repository_site(self):
        value = discovery("next-markdown-blog")
        self.assertEqual(value["contentSource"]["kind"], "repository")
        self.assertEqual(value["supportLevel"], "agentic")

    def test_no_projection_ever_says_unsupported(self):
        """Plan C3: older scans said "unsupported" for any site without a native adapter."""
        for payload in ({}, {"repository_discovery": {"support_level": "unsupported", "discovery_complete": True}},
                        {"repository_discovery": {"support_level": "adapter_available"}}):
            with self.subTest(payload=payload):
                value = discovery_snapshot(payload)
                self.assertNotIn("unsupported", json.dumps({k: value[k] for k in ("supportLevel", "setupTier")}))
        self.assertEqual(discovery_snapshot({"repository_discovery": {"support_level": "unsupported"}})["supportLevel"], "agentic")
        self.assertEqual(discovery_snapshot({"repository_discovery": {"support_level": "adapter_available"}})["supportLevel"], "native")
        self.assertEqual(discovery_snapshot({})["supportLevel"], "not_checked")

    def test_malformed_classifications_are_ignored(self):
        for value in ({"version": 2, "kind": "cms", "cms": "wordpress"}, {"version": 1, "kind": "cms", "cms": "joomla"}, "cms"):
            with self.subTest(value=value):
                self.assertNotEqual(discovery_snapshot({"repository_inventory": {"content_source": value}})["contentSource"].get("kind"), "cms")


class PrepareStopTests(SimpleTestCase):
    def setUp(self):
        self.refund = Mock()
        self.alert = Mock()
        self.lead = Mock()
        for target, mock in (("content_factory.website_first_article_refunds.reserve_refund", self.refund),
                             ("content_factory.founder_journey_alerts.queue_founder_journey_alert", self.alert),
                             ("content_factory.website_cms_content.record_lead", self.lead)):
            patcher = patch(target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def advance(self, name, step="setup", observed=None):
        op = operation_fixtures.PrepareJourneyTests().operation()
        op.connection.organization = SimpleNamespace(pk=7, domain="example.com")
        config = SimpleNamespace(website_connection=op.connection, website_connection_id=op.connection_id)
        context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=SimpleNamespace(pk=7))
        scan = discovery(name)
        journey = {"nextAction": {"id": step}, "capabilities": {"canPublishArticle": False}, "discovery": scan,
                   "repository": {"observedSourceSha": scan["sourceSha"] if observed is None else observed}}
        handler = Mock(return_value={"status": "completed", "run_id": "setup-run"})
        with patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(context, config, SimpleNamespace(pk=4))), \
                patch.object(prepare, "journey_for_context", return_value=journey), \
                patch.object(prepare, "_selected_setup_child", return_value=None), \
                patch.object(prepare, "_return_to_open_setup_pr", return_value=None), \
                patch("content_factory.website_connections.settled_setup_run", return_value=None), \
                patch("content_factory.website_prepare_sticky_stop._relevant", return_value=([], [])), \
                patch("content_factory.website_prepare_sticky_stop._stop_source", return_value=""), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda operation, **kwargs: operation):
            result = prepare.advance_prepare(op.pk, handlers={step: handler})
        return result, handler

    def test_wordpress_and_headless_cms_end_in_cannot_yet_before_any_setup_run_or_charge(self):
        for name in ("wordpress-theme", "next-contentful-blog"):
            with self.subTest(name=name):
                self.refund.reset_mock(), self.alert.reset_mock(), self.lead.reset_mock()
                op, setup = self.advance(name)
                setup.assert_not_called()
                self.assertNotIn("setup_run_id", op.receipt)
                self.assertEqual(op.state, "stopped")
                self.assertEqual(op.receipt["status"], "cant_be_done_yet")
                # A closed "can't be done yet" reason, not a failure registry code.
                self.assertEqual(op.receipt["reason_code"], "")
                self.assertEqual(op.receipt["cant_be_done_yet"]["reason"], "cms_content")
                self.assertEqual(op.receipt["cant_be_done_yet"]["stage"], "repository_selection")
                self.assertIsNone(op.next_attempt_at)
                self.assertNotIn("user_action", op.receipt)
                # Any reservation is released; interest is captured; staff are not alerted.
                self.refund.assert_called_once()
                self.lead.assert_called_once()
                self.alert.assert_not_called()
                self.assertIn("Nothing was charged", op.receipt["message"])
                from .website_operations import prepare_receipt
                self.assertFalse(prepare_receipt(op)["owner_can_retry"])

    def test_the_scan_itself_still_runs(self):
        op, scan = self.advance("wordpress-theme", step="scan")
        scan.assert_called_once()
        self.assertNotEqual(op.receipt.get("status"), "cant_be_done_yet")

    def test_a_repository_blog_goes_on_to_setup(self):
        op, setup = self.advance("next-markdown-blog")
        setup.assert_called_once()
        self.assertEqual(op.receipt["setup_run_id"], "setup-run")

    def test_a_scan_of_an_older_source_does_not_stop_the_current_one(self):
        op, setup = self.advance("wordpress-theme", observed="f" * 40)
        setup.assert_called_once()
        self.assertNotEqual(op.receipt.get("status"), "cant_be_done_yet")

    def test_an_incomplete_scan_never_stops(self):
        value = discovery("wordpress-theme")
        self.assertIsNotNone(cms_content.classification(value))
        self.assertIsNone(cms_content.classification({**value, "complete": False}))


class LeadTests(SimpleTestCase):
    def test_the_lead_uses_the_website_lead_row_with_the_cms_as_host_class(self):
        user = SimpleNamespace(pk=4)
        organization = SimpleNamespace(pk=7, billing_user_id=4, domain="https://www.Example.com/")
        with patch("django.contrib.auth.get_user_model") as users, patch("founder_tools.website_leads.record_lead") as record:
            users.return_value.objects.filter.return_value.first.return_value = user
            cms_content.record_lead(organization, cms="wordpress")
        lead = record.call_args.args[1]
        self.assertEqual((lead["reason"], lead["domain"], lead["hostClass"], lead["notify"]),
                         ("cms_content", "example.com", "wordpress", False))
        self.assertEqual(record.call_args.kwargs["source"], "repository_selection.cms_content")
