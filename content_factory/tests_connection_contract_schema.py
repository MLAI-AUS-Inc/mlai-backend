"""contracts/connection-contract-v1.schema.json lists exactly CONNECTION_FIELDS.

Content Factory pins a copy of this schema; when a field is added here, its
request builders' test fails until connection_contract(store) sends it.
"""
import json
from pathlib import Path

from django.test import SimpleTestCase

from .website_contract import CONNECTION_FIELDS, connection_contract

SCHEMA = Path(__file__).resolve().parents[1] / "contracts" / "connection-contract-v1.schema.json"


class ConnectionContractSchemaTests(SimpleTestCase):
    def setUp(self):
        self.schema = json.loads(SCHEMA.read_text())

    def test_schema_lists_every_connection_field(self):
        self.assertEqual(tuple(self.schema["x-connection-fields"]), CONNECTION_FIELDS)
        self.assertEqual(set(self.schema["properties"]), set(CONNECTION_FIELDS))

    def test_required_fields_are_the_ones_connection_contract_requires(self):
        full = {"website_connection_id": "00000000-0000-0000-0000-000000000001", "connection_generation": 2,
                "connection_target_id": "native", "repository_id": 9}
        self.assertEqual(connection_contract(full), full)
        minimal = {key: full[key] for key in self.schema["required"]}
        self.assertEqual(connection_contract(minimal), minimal)
        # Optional in the schema means connection_contract also accepts it missing.
        self.assertEqual(set(self.schema["required"]), {"website_connection_id", "connection_generation"})
