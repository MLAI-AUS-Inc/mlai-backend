"""Closed provenance metadata and actual runner-byte checks without network."""
from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from scripts import check_sealed_verifier_contract as provenance
from scripts.check_failure_contract import ContractCheckError


class SealedProvenanceTests(SimpleTestCase):
    def prepared(self, root):
        # Isolated contract mechanics use known toy runner bytes, never a claim
        # that this test fetched or executed the private production verifier.
        runner = b"# isolated unit runner; never executed\n"
        lock = json.loads((provenance.ROOT / provenance.LOCK).read_bytes())
        files = {provenance.RUNNER: runner}
        for path, source in provenance.ARTIFACTS.items():
            raw = (provenance.ROOT / path).read_bytes()
            if path == provenance.MANIFEST:
                manifest = json.loads(raw)
                manifest["runner_sha256"] = hashlib.sha256(runner).hexdigest()
                lock["runner"]["sha256"] = manifest["runner_sha256"]
                raw = json.dumps(manifest).encode()
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            lock["artifacts"][path]["sha256"] = hashlib.sha256(raw).hexdigest()
            files[source] = raw
        self.save_lock(root, lock)
        return lock, files

    def save_lock(self, root, lock):
        (root / provenance.LOCK).write_text(json.dumps(lock))

    def test_checked_in_bytes_and_manifest_are_valid_without_claiming_remote_provenance(self):
        fetch = Mock(side_effect=AssertionError("Network must not be used"))
        lock = provenance.verify(fetch=fetch)
        self.assertEqual(lock["source_revision"], "6274430937df29a69e7fd59caf4b68705e99d56d")
        fetch.assert_not_called()

    def test_remote_fetches_all_artifacts_and_actual_runner_at_one_exact_sha(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); lock, files = self.prepared(root)
            before = {path: (root / path).read_bytes() for path in (*provenance.ARTIFACTS, provenance.LOCK)}
            fetch = Mock(side_effect=lambda path, revision, token: files[path])
            self.assertEqual(provenance.verify(root, remote=True, token="synthetic-read-token", fetch=fetch), lock)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [*provenance.ARTIFACTS.values(), provenance.RUNNER])
            self.assertTrue(all(call.args[1:] == (lock["source_revision"], "synthetic-read-token") for call in fetch.call_args_list))
            self.assertEqual(before, {path: (root / path).read_bytes() for path in before})

    def test_mutable_or_noncanonical_source_revision_is_rejected_before_fetch(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); original, _ = self.prepared(root)
            for revision in ("main", "refs/heads/main", "7e827bf", "A" * 40, "a" * 64, None, True):
                lock = {**original, "source_revision": revision}; self.save_lock(root, lock)
                fetch = Mock()
                with self.subTest(revision=revision), self.assertRaises(ContractCheckError):
                    provenance.verify(root, remote=True, token="synthetic", fetch=fetch)
                fetch.assert_not_called()

    def test_closed_lock_and_exact_repository_artifact_paths_reject_changed_metadata(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); original, _ = self.prepared(root)
            changes = [lambda lock: lock.update(schema_version=True), lambda lock: lock.update(source_repository="other/repo"),
                lambda lock: lock.update(extra=True), lambda lock: lock.pop("runner"),
                lambda lock: lock["runner"].update(source_path="../runner.py"),
                lambda lock: lock["runner"].update(sha256="x" * 64),
                lambda lock: lock["artifacts"].pop(provenance.MANIFEST),
                lambda lock: lock["artifacts"].update({"../arbitrary": {"source_path": "secret", "sha256": "a" * 64}}),
                lambda lock: lock["artifacts"][provenance.MANIFEST].update(source_path="other/manifest.json"),
                lambda lock: lock["artifacts"][provenance.MANIFEST].update(extra="unrepresented"),
                lambda lock: lock["artifacts"][provenance.MANIFEST].update(sha256=True)]
            for index, change in enumerate(changes):
                lock = deepcopy(original); change(lock); self.save_lock(root, lock)
                fetch = Mock()
                with self.subTest(index=index), self.assertRaises(ContractCheckError):
                    provenance.verify(root, remote=True, token="synthetic", fetch=fetch)
                fetch.assert_not_called()

    def test_each_local_artifact_must_match_exact_locked_bytes(self):
        for path in provenance.ARTIFACTS:
            with self.subTest(path=path), TemporaryDirectory() as folder:
                root = Path(folder); self.prepared(root)
                (root / path).write_bytes(b"changed local fixture")
                fetch = Mock()
                with self.assertRaisesRegex(ContractCheckError, "local_bytes_changed"):
                    provenance.verify(root, remote=True, token="synthetic", fetch=fetch)
                fetch.assert_not_called()

    def test_manifest_metadata_remains_closed_when_its_lock_hash_is_recomputed(self):
        changes = [{"schema_version": True}, {"proof_schema_version": 2}, {"proof_schema_version": 3.0},
            {"runner_path": "other.py"}, {"runner_sha256": "x" * 64}, {"profile": "custom"},
            {"python_version": "3.12"}, {"playwright_version": "other"}, {"extra": "field"}]
        for change in changes:
            with self.subTest(change=change), TemporaryDirectory() as folder:
                root = Path(folder); lock, _ = self.prepared(root)
                path = root / provenance.MANIFEST
                manifest = {**json.loads(path.read_bytes()), **change}
                raw = json.dumps(manifest).encode(); path.write_bytes(raw)
                lock["artifacts"][provenance.MANIFEST]["sha256"] = hashlib.sha256(raw).hexdigest()
                self.save_lock(root, lock)
                with self.assertRaisesRegex(ContractCheckError, "invalid_manifest"): provenance.verify(root)

    def test_manifest_and_runner_lock_hash_must_agree(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); lock, _ = self.prepared(root)
            lock["runner"]["sha256"] = "a" * 64; self.save_lock(root, lock)
            with self.assertRaisesRegex(ContractCheckError, "runner_metadata_changed"): provenance.verify(root)

    def test_missing_token_fails_before_remote_access_even_with_valid_local_bytes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); self.prepared(root)
            for token in (None, "", "  ", True):
                fetch = Mock()
                with self.subTest(token_type=type(token).__name__), self.assertRaisesRegex(ContractCheckError, "token_required"):
                    provenance.verify(root, remote=True, token=token, fetch=fetch)
                fetch.assert_not_called()

    def test_each_private_artifact_must_match_bytes_even_when_local_hash_is_valid(self):
        for source in provenance.ARTIFACTS.values():
            with self.subTest(source=source), TemporaryDirectory() as folder:
                root = Path(folder); _, files = self.prepared(root)
                files[source] += b"remote drift"
                fetch = Mock(side_effect=lambda path, revision, token: files[path])
                with self.assertRaisesRegex(ContractCheckError, "private_source_bytes_changed"):
                    provenance.verify(root, remote=True, token="synthetic", fetch=fetch)

    def test_actual_runner_bytes_must_match_manifest_not_only_another_descriptor(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); _, files = self.prepared(root)
            for raw in (b"different runner", files[provenance.MANIFEST], None):
                files[provenance.RUNNER] = raw
                fetch = Mock(side_effect=lambda path, revision, token: files[path])
                with self.subTest(raw_type=type(raw).__name__), self.assertRaisesRegex(ContractCheckError, "private_runner_bytes_changed"):
                    provenance.verify(root, remote=True, token="synthetic", fetch=fetch)
                self.assertEqual(fetch.call_count, 4)

    def test_duplicate_metadata_keys_are_rejected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); lock, _ = self.prepared(root)
            raw = json.dumps(lock)
            (root / provenance.LOCK).write_text('{"schema_version":1,' + raw[1:])
            with self.assertRaisesRegex(ContractCheckError, "duplicate_metadata"): provenance.verify(root)

    def test_cli_is_explicit_about_offline_limit_and_never_prints_private_errors(self):
        for remote in (False, True):
            output = io.StringIO()
            with patch("sys.argv", ["check"] + (["--remote"] if remote else [])), patch.object(provenance, "verify", return_value={}), redirect_stdout(output):
                self.assertEqual(provenance.main(), 0)
            self.assertIn("pinned private artifacts" if remote else "were not rechecked", output.getvalue())
        output = io.StringIO()
        with patch("sys.argv", ["check", "--remote"]), patch.object(provenance, "verify", side_effect=RuntimeError("provider-secret-token private-response-body")), redirect_stdout(output):
            self.assertEqual(provenance.main(), 1)
        self.assertNotIn("provider-secret-token", output.getvalue())
        self.assertNotIn("private-response-body", output.getvalue())
