"""The shared native identity algorithm stays pinned to its producer examples."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from scripts.check_failure_contract import ContractCheckError
from scripts.check_native_target_identity_contract import ROOT, verify


class NativeTargetIdentityContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "contracts").mkdir()
        for path in (ROOT / "contracts").glob("native-target-identity-v1.*.json"):
            shutil.copyfile(path, self.root / "contracts" / path.name)
        self.path = self.root / "contracts/native-target-identity-v1.lock.json"
        self.lock = json.loads(self.path.read_text())

    def test_remote_read_uses_exact_allowlisted_path_and_immutable_revision(self):
        observed = []
        def fetch(path, revision, token):
            observed.append((path, revision, token))
            return (self.root / path.removeprefix("backend/")).read_bytes()
        self.assertEqual(verify(self.root, remote=True, token="synthetic-read-token", fetch=fetch), self.lock)
        self.assertEqual(observed, [(row["source_path"], self.lock["source_revision"], "synthetic-read-token") for row in self.lock["files"]])

    def test_missing_access_or_changed_producer_bytes_fail_closed(self):
        with self.assertRaises(ContractCheckError):
            verify(self.root, remote=True)
        with self.assertRaises(ContractCheckError):
            verify(self.root, remote=True, token="synthetic-read-token", fetch=lambda *args: b"different")
        (self.root / "contracts/native-target-identity-v1.example.json").write_text("{}")
        with self.assertRaises(ContractCheckError):
            verify(self.root)

    def test_lock_cannot_select_mutable_refs_other_repos_or_arbitrary_files(self):
        mutations = [lambda value: value.update(source_revision="main"),
            lambda value: value.update(schema_version=True),
            lambda value: value.update(source_repository="elsewhere/site"),
            lambda value: value.update(extra=True),
            lambda value: value["files"].append(deepcopy(value["files"][0])),
            lambda value: value["files"][0].update(local_path="../private"),
            lambda value: value["files"][0].update(source_path="backend/../private"),
            lambda value: value["files"][0].update(sha256="not-a-digest")]
        for mutate in mutations:
            value = deepcopy(self.lock)
            mutate(value)
            self.path.write_text(json.dumps(value))
            with self.assertRaises(ContractCheckError):
                verify(self.root)
