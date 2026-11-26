"""A repairable, owner-retryable stop never carries an earlier stop's channel_options; no database."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from .website_operations import prepare_receipt, without_stale_channel_options

STALE = {"status": "needs_attention", "failure_class": "repairable", "reason_code": "DIRECTORY_ROUTE_SOURCE_MISSING",
         "message": "The blog page could not be found.", "channel_options": ["portable"],
         "user_action": {"id": "retry", "label": "Try again"}}


class StaleChannelOptionsTests(SimpleTestCase):
    def test_repairable_stop_drops_an_earlier_offer(self):
        self.assertNotIn("channel_options", without_stale_channel_options(STALE))

    def test_current_channel_offers_are_kept(self):
        choose = {**STALE, "user_action": {"id": "choose_article_channel", "label": "Choose an articles channel"}}
        self.assertEqual(without_stale_channel_options(choose)["channel_options"], ["portable"])
        unsupported = {**STALE, "failure_class": "unsupported", "user_action": {"id": "x"}}
        self.assertIn("channel_options", without_stale_channel_options(unsupported))
        nested = {**STALE, "failure_class": "", "failure": {"failure_class": "unsupported"}}
        self.assertIn("channel_options", without_stale_channel_options(nested))
        closed = {**STALE, "failure_class": "", "status": "cant_be_done_yet"}
        self.assertIn("channel_options", without_stale_channel_options(closed))

    def test_input_is_not_mutated(self):
        receipt = dict(STALE)
        without_stale_channel_options(receipt)
        self.assertEqual(receipt["channel_options"], ["portable"])
        self.assertEqual(without_stale_channel_options(None), {})

    def test_prepare_receipt_projects_the_fix_for_saved_receipts(self):
        op = SimpleNamespace(state="needs_attention", receipt=dict(STALE), next_attempt_at=None)
        projected = prepare_receipt(op)
        self.assertNotIn("channel_options", projected)
        self.assertEqual(op.receipt["channel_options"], ["portable"])
