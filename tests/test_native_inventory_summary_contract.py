"""The display consumer cannot silently drift from its canonical producer."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from scripts.check_native_inventory_summary_contract import verify, ROOT
from scripts.check_failure_contract import ContractCheckError


class NativeInventorySummaryContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "contracts").mkdir()
        for path in (ROOT / "contracts").glob("native-inventory-summary-v1.*.json"):
            shutil.copyfile(path, self.root / "contracts" / path.name)
        self.path = self.root / "contracts/native-inventory-summary-v1.lock.json"
        self.lock = json.loads(self.path.read_text())

    def test_exact_private_producer_paths_and_commit_are_read_for_both_files(self):
        observed = []
        def fetch(path, revision, token):
            observed.append((path, revision, token))
            return (self.root / path.removeprefix("backend/")).read_bytes()
        self.assertEqual(verify(self.root, remote=True, token="synthetic-read-token", fetch=fetch), self.lock)
        self.assertEqual(observed, [(row["source_path"], self.lock["source_revision"], "synthetic-read-token") for row in self.lock["files"]])

    def test_missing_secret_changed_local_bytes_and_remote_mismatch_fail_closed(self):
        with self.assertRaises(ContractCheckError): verify(self.root, remote=True)
        with self.assertRaises(ContractCheckError): verify(self.root, remote=True, token="synthetic-read-token", fetch=lambda *a: b"other")
        (self.root / "contracts/native-inventory-summary-v1.example.json").write_text("{}")
        with self.assertRaises(ContractCheckError): verify(self.root)

    def test_future_mutable_duplicate_and_escaping_locks_are_rejected(self):
        mutations = [lambda value: value.update(source_revision="main"), lambda value: value.update(schema_version=True),
            lambda value: value.update(source_repository="elsewhere/site"), lambda value: value.update(unsafe=True),
            lambda value: value["files"][0].update(local_path="../private"),
            lambda value: value["files"][0].update(source_path="backend/../private"),
            lambda value: value["files"].__setitem__(1, deepcopy(value["files"][0]))]
        for mutate in mutations:
            value = deepcopy(self.lock)
            mutate(value)
            self.path.write_text(json.dumps(value))
            with self.assertRaises(ContractCheckError): verify(self.root)
