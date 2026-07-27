"""Guards for the Customer.io template docs that get pasted into the CIO UI."""
import json
from pathlib import Path

from django.test import SimpleTestCase

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "docs" / "customerio-daily-topics-email.html"


class CustomerioTemplateDocTests(SimpleTestCase):
    def test_daily_topics_template_is_pure_ascii(self):
        # Customer.io's template storage has mangled literal UTF-8 into
        # Mac-Roman mojibake in received emails. Entities and \uXXXX escapes
        # survive every hop; raw multibyte characters must never come back.
        content = TEMPLATE_PATH.read_text(encoding="utf-8")
        offending = sorted(
            {
                f"line {line_number}: {char!r}"
                for line_number, line in enumerate(content.splitlines(), start=1)
                for char in line
                if ord(char) > 127
            }
        )
        self.assertEqual(offending, [])

    def test_sample_test_payload_is_valid_json_with_analytics(self):
        content = TEMPLATE_PATH.read_text(encoding="utf-8")
        marker = "SAMPLE TEST PAYLOAD"
        self.assertIn(marker, content)
        block = content[content.index(marker):]
        start = block.index("{")
        end = block.rindex("}") + 1
        payload = json.loads(block[start:end])
        self.assertTrue(payload["analytics"]["available"])
        self.assertEqual(len(payload["topics"]), 3)
        self.assertIn("·", payload["analytics"]["summary_line"])
