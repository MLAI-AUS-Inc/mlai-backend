"""Shared client/server validation fixtures; no database or service access."""
import json
from pathlib import Path
from django.test import SimpleTestCase
from crm.segments import SegmentError, validate_definition


class AudienceContractTests(SimpleTestCase):
    def test_shared_valid_contracts(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/audience-contract.json").read_text())
        for fixture in fixtures["valid"]:
            with self.subTest(fixture["name"]):
                validate_definition(fixture["definition"])

    def test_shared_invalid_contracts(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/audience-contract.json").read_text())
        for fixture in fixtures["invalid"]:
            with self.subTest(fixture["name"]), self.assertRaises(SegmentError):
                validate_definition(fixture["definition"])
