"""Source coverage regressions; these tests do not construct a database."""
import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from startup_updates.source_evidence import period_gmail_bundle, source_receipt
from startup_updates.manual_documents import parse_manual_document


class SourceEvidenceTests(unittest.TestCase):
    def test_february_thread_survives_later_replies_without_future_claims(self):
        messages = [
            {"message_id": "feb", "internal_date": "2026-02-12T12:00:00+00:00", "cleaned_text": "February pilot signed."},
            {"message_id": "sep", "internal_date": "2026-09-05T12:00:00+00:00", "cleaned_text": "September pilot cancelled."},
        ]
        thread = SimpleNamespace(gmail_thread_id="thread", message_payloads=messages)
        bundle = period_gmail_bundle(thread, start=datetime(2026, 2, 1, tzinfo=timezone.utc), end=datetime(2026, 2, 28, 23, 59, 59, tzinfo=timezone.utc), attachments=[])
        self.assertEqual(bundle["source_message_ids"], ["feb"])
        self.assertIn("pilot signed", bundle["cleaned_text"])
        self.assertNotIn("cancelled", bundle["cleaned_text"])
        self.assertEqual(bundle["omitted_message_count"], 0)

    def test_receipts_change_with_period_source_or_extractor_version(self):
        run = SimpleNamespace(run_request={"draft_months": ["2026-02-01"]})
        bundle = {"text": "Original February result"}
        before = source_receipt(run, "notion", "page", bundle)["fingerprint"]
        self.assertEqual(before, source_receipt(run, "notion", "page", bundle)["fingerprint"])
        self.assertNotEqual(before, source_receipt(run, "notion", "page", {"text": "Edited result"})["fingerprint"])
        run.run_request["draft_months"] = ["2026-08-01"]
        self.assertNotEqual(before, source_receipt(run, "notion", "page", bundle)["fingerprint"])
        run.run_request["draft_months"] = ["2026-02-01"]
        with patch("startup_updates.source_evidence.EXTRACTION_VERSION", "future-parser"):
            self.assertNotEqual(before, source_receipt(run, "notion", "page", bundle)["fingerprint"])

    def test_parser_cache_does_not_change_the_underlying_source_fingerprint(self):
        run = SimpleNamespace(run_request={})
        bundle = {"attachments": [{"id": 1, "raw_content_base64": "YWJj", "extracted_text": "", "parse_notes": ""}]}
        before = source_receipt(run, "gmail", "thread", bundle)
        bundle["attachments"][0].update(extracted_text="Table fact", parse_notes="docx_parsed", extraction_status="processed")
        after = source_receipt(run, "gmail", "thread", bundle)
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertNotIn("raw_content_base64", after["bundle"]["attachments"][0])
        self.assertIn("binary_hash", after["bundle"]["attachments"][0])

    def test_uploaded_docx_table_after_old_character_limit_is_extracted(self):
        from docx import Document
        document = Document()
        document.add_paragraph("Background. " * 2500)
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "2026-08-23 milestone"
        table.rows[0].cells[1].text = "First accessible offline release shipped."
        stream = io.BytesIO()
        document.save(stream)
        parsed = parse_manual_document(filename="evidence.docx", content_type="", raw_bytes=stream.getvalue())
        self.assertEqual(parsed.extraction_status, "processed")
        self.assertGreater(parsed.extracted_text.index("First accessible offline release"), 12000)
        self.assertIn("2026-08-23 milestone | First accessible offline release shipped.", parsed.extracted_text)

    def test_spreadsheet_blank_column_does_not_shift_its_metric_label(self):
        parsed = parse_manual_document(filename="evidence.csv", content_type="text/csv", raw_bytes=b"Month,Revenue,Customers\nAugust,,7")
        self.assertIn("August | [empty] | 7", parsed.extracted_text)


class NotionCoverageTests(unittest.TestCase):
    def test_nested_blocks_and_fact_beyond_eighty_blocks_are_kept(self):
        from startup_updates.api_views import _build_notion_page_bundle
        blocks = [{"id": str(i), "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": f"Paragraph {i}"}]}} for i in range(101)]
        blocks[0]["has_children"] = True
        nested = {"id": "nested", "type": "table_row", "table_row": {"cells": [[{"plain_text": "February 2026"}], [{"plain_text": "Retention review completed"}]]}}
        with patch("startup_updates.api_views._fetch_notion_children", side_effect=lambda connection, block_id: blocks if block_id == "page" else [nested]):
            bundle = _build_notion_page_bundle(None, {"id": "page", "last_edited_time": "2026-09-01T00:00:00Z"})
        self.assertIn("Paragraph 100", bundle["cleaned_text"])
        self.assertIn("February 2026 | Retention review completed", bundle["cleaned_text"])
        self.assertEqual(len(bundle["source_block_ids"]), 102)
        self.assertEqual(bundle["omitted_block_count"], 0)
        self.assertEqual(bundle["last_edited_time"], "2026-09-01T00:00:00Z")

    def test_nonadvancing_notion_cursor_fails_instead_of_silent_truncation(self):
        from startup_updates.api_views import _fetch_notion_children
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"results": [], "has_more": True, "next_cursor": "stuck"})
        with patch("startup_updates.api_views.requests.get", return_value=response), patch("startup_updates.api_views._notion_headers", return_value={}):
            with self.assertRaisesRegex(ValueError, "did not advance"):
                _fetch_notion_children(None, "page")

class AccountingReportTests(unittest.TestCase):
    def report(self, rows):
        return {"Reports": [{"Rows": [{"RowType": "Section", "Title": "Income", "Rows": [
            {"RowType": "SummaryRow", "Cells": [{"Value": label}, {"Value": str(amount)}]} for label, amount in rows
        ]}]}]}

    def test_report_revenue_uses_total_and_keeps_negative_adjustments(self):
        from startup_updates.services import _parse_xero_profit_and_loss_report
        parsed = _parse_xero_profit_and_loss_report(self.report([("Other Income", 500), ("Total Income", -50), ("Total Operating Expenses", -20)]))
        self.assertEqual(parsed["revenue"]["amount"], -50)
        self.assertEqual(parsed["monthly_costs"]["amount"], -20)

    def test_single_income_category_is_not_whole_business_revenue(self):
        from startup_updates.services import _parse_xero_profit_and_loss_report
        parsed = _parse_xero_profit_and_loss_report(self.report([("Other Income", 500)]))
        self.assertIsNone(parsed["revenue"])

class ClassificationContractTests(unittest.TestCase):
    def test_cache_changes_with_period_or_startup_context_but_not_run_id(self):
        from startup_updates.source_evidence import classification_version
        run = SimpleNamespace(run_id="first", run_request={"draft_months": ["2026-02-01"], "startup_context": {"company_name": "Original"}})
        original = classification_version(run)
        run.run_id = "retry"
        self.assertEqual(classification_version(run), original)
        run.run_request["startup_context"]["company_name"] = "Renamed"
        self.assertNotEqual(classification_version(run), original)
        renamed = classification_version(run)
        run.run_request["draft_months"] = ["2026-08-01"]
        self.assertNotEqual(classification_version(run), renamed)
