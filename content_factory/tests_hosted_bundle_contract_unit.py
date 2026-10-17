"""Prepared public files have no owner, provider, billing or live authority."""

from copy import deepcopy
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from .channel_artifact_contract import digest
from .hosted_bundle_contract import BundleFile, HostedBundle, HostedBundleError, load_export, validate_bundle
from .publishing_channel_adapters import HostedChannel
from scripts.check_hosted_bundle_contract import verify, ROOT
from scripts.check_failure_contract import ContractCheckError


class HostedBundleContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / "contracts/hosted-static-bundle-v1.example.json").read_text())
        self.value = HostedBundle(deepcopy(self.fixture["manifest"]), tuple(BundleFile(row["path"], row["media_type"],
            self.fixture["public_files"][row["path"]].encode()) for row in self.fixture["manifest"]["files"]))

    def changed_files(self, files):
        manifest = deepcopy(self.value.manifest)
        manifest["files"] = [{"path": item.path, "media_type": item.media_type, "bytes": len(item.content),
            "sha256": hashlib.sha256(item.content).hexdigest()} for item in files]
        manifest["digest"] = digest({key: value for key, value in manifest.items() if key != "digest"})
        return HostedBundle(manifest, tuple(files))

    def test_actual_producer_fixture_decodes_with_exact_public_bytes_and_private_manifest_separation(self):
        raw = self.value.export_zip()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.fixture["zip_sha256"])
        decoded = load_export(self.fixture["manifest"], raw)
        self.assertEqual(decoded, self.value)
        self.assertIsNot(decoded.manifest, self.fixture["manifest"])
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            self.assertEqual(archive.namelist(), [item.path for item in self.value.files])
            self.assertNotIn("manifest.json", archive.namelist())
            self.assertTrue(all(archive.read(item.path) == item.content for item in self.value.files))
        for private in (b"owner_user_id", b"billing_request_id", b"connection_id", b"vibe-article:7:original-request"):
            self.assertNotIn(private, raw)
        self.assertEqual(repr(decoded), "HostedBundle()")

    def test_valid_static_candidate_does_not_enable_hosted_provider_effects(self):
        self.assertIs(validate_bundle(self.value), self.value)
        self.assertEqual(HostedChannel.name, "hosted")
        self.assertFalse(HostedChannel.effect_capabilities)
        self.assertFalse({"approved", "live", "publishingReady", "proof"} & self.value.manifest.keys())

    def test_manifest_cannot_claim_authority_or_change_original_file_identity(self):
        for changes in ({"approved": True}, {"schema_version": True}, {"scope_sha256": "private"},
                {"public_origin": "https://fixture.test/base/"}, {"digest": "a" * 64}):
            with self.subTest(fields=sorted(changes)), self.assertRaisesRegex(HostedBundleError, "^Invalid hosted static bundle$"):
                load_export({**self.value.manifest, **changes}, self.value.export_zip())

    def test_exact_shell_and_styles_are_checked_even_after_all_file_hashes_are_recomputed(self):
        detail = self.value.manifest["articles"][0]["path"] + "index.html"
        for marker in (b"<style>article{display:none}</style>", b'<meta http-equiv="refresh" content="0;url=https://outside.test/">'):
            files = [replace(item, content=item.content.replace(b"</main>", marker + b"</main>")) if item.path == detail else item for item in self.value.files]
            with self.subTest(marker=marker[:6]), self.assertRaises(HostedBundleError): self.changed_files(files).export_zip()
        old = next(item for item in self.value.files if item.path.endswith(".css"))
        content = old.content + b"article{display:none}"
        path = "assets/" + hashlib.sha256(content).hexdigest() + ".css"
        files = sorted([BundleFile(path, item.media_type, content) if item == old else
            replace(item, content=item.content.replace(old.path.encode(), path.encode())) if item.path.endswith(".html") else item
            for item in self.value.files], key=lambda item: item.path)
        with self.assertRaises(HostedBundleError): self.changed_files(files).export_zip()

    def test_archive_metadata_members_bounds_and_trailing_bytes_are_refused(self):
        for kind in ("compressed", "symlink", "duplicate", "traversal", "missing", "private", "trailing", "corrupt"):
            output = BytesIO()
            if kind in {"trailing", "corrupt"}:
                raw = self.value.export_zip()
                raw = raw + b"synthetic-private-suffix" if kind == "trailing" else raw[:40] + bytes([raw[40] ^ 255]) + raw[41:]
            else:
                with zipfile.ZipFile(output, "w") as archive:
                    for index, item in enumerate(self.value.files):
                        if kind == "missing" and index == 0: continue
                        info = zipfile.ZipInfo("../private" if kind == "traversal" and index == 0 else item.path, date_time=(1980, 1, 1, 0, 0, 0))
                        info.create_system = 3
                        info.external_attr = (0o120777 if kind == "symlink" and index == 0 else 0o100644) << 16
                        info.compress_type = zipfile.ZIP_DEFLATED if kind == "compressed" else zipfile.ZIP_STORED
                        archive.writestr(info, item.content)
                        if kind == "duplicate" and index == 0:
                            with self.assertWarns(UserWarning): archive.writestr(info, item.content)
                    if kind == "private": archive.writestr("manifest.json", json.dumps(self.value.manifest))
                raw = output.getvalue()
            with self.subTest(kind=kind), self.assertRaisesRegex(HostedBundleError, "^Invalid hosted static bundle$"):
                load_export(self.value.manifest, raw)

    def test_schema_and_synthetic_example_have_exact_closed_fields(self):
        schema = json.loads((ROOT / "contracts/hosted-static-bundle-v1.schema.json").read_text())
        self.assertEqual(schema["required"], sorted(self.value.manifest))
        self.assertIs(schema["additionalProperties"], False)
        for field in ("articles", "files"):
            item = schema["properties"][field]["items"]
            self.assertIs(item["additionalProperties"], False)
            self.assertTrue(all(sorted(row) == item["required"] for row in self.value.manifest[field]))

    def test_actual_image_bytes_are_decoded_and_mime_or_content_changes_are_refused(self):
        from PIL import Image
        output = BytesIO()
        Image.new("RGB", (8, 8), (20, 60, 90)).save(output, format="PNG")
        content = output.getvalue()
        path = "assets/" + hashlib.sha256(content).hexdigest() + ".png"
        files = sorted([*self.value.files, BundleFile(path, "image/png", content)], key=lambda item: item.path)
        candidate = self.changed_files(files)
        self.assertEqual(load_export(candidate.manifest, candidate.export_zip()), candidate)
        for name, media_type, body in ((path, "image/jpeg", content), (path.replace(".png", ".jpg"), "image/jpeg", content),
                (path, "image/png", b"private-image-not-png")):
            changed = [*self.value.files, BundleFile(name, media_type, body)]
            with self.subTest(media_type=media_type), self.assertRaises(HostedBundleError):
                self.changed_files(sorted(changed, key=lambda item: item.path)).export_zip()

    def test_palette_revision_cannot_be_downgraded_after_manifest_rehashing(self):
        self.assertEqual(self.value.manifest["renderer_version"], 2)
        manifest = deepcopy(self.value.manifest)
        manifest["renderer_version"] = 1
        manifest["digest"] = digest({key: value for key, value in manifest.items() if key != "digest"})
        with self.assertRaises(HostedBundleError): HostedBundle(manifest, self.value.files).export_zip()

    def test_readdressed_white_on_white_copy_fails_the_actual_backend_decoder(self):
        old = next(item for item in self.value.files if item.path.endswith(".css"))
        content = old.content.replace(b"background:#ffffff;color:#172033;font:", b"background:#ffffff;color:#ffffff;font:")
        self.assertNotEqual(content, old.content)
        path = "assets/" + hashlib.sha256(content).hexdigest() + ".css"
        files = sorted([BundleFile(path, item.media_type, content) if item == old else
            replace(item, content=item.content.replace(old.path.encode(), path.encode())) if item.path.endswith(".html") else item
            for item in self.value.files], key=lambda item: item.path)
        with self.assertRaises(HostedBundleError): self.changed_files(files).export_zip()

    def test_immutable_producer_bytes_require_exact_source_revision_paths_and_read_access(self):
        lock = verify()
        producer = {row["source_path"]: (ROOT / row["local_path"]).read_bytes() for row in lock["files"]}
        def read(path, revision, token):
            self.assertEqual(revision, lock["source_revision"])
            self.assertEqual(token, "synthetic-read-only-token")
            return producer[path]
        self.assertEqual(verify(remote=True, token="synthetic-read-only-token", fetch=read), lock)
        with self.assertRaises(ContractCheckError): verify(remote=True, fetch=read)
        with self.assertRaises(ContractCheckError): verify(remote=True, token="synthetic-read-only-token", fetch=lambda *args: b"changed")

    def test_changed_vendored_bytes_duplicate_mappings_and_moved_producer_paths_fail_closed(self):
        lock = verify()
        for kind in ("bytes", "duplicate", "path", "revision"):
            with tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                for row in lock["files"]:
                    target = root / row["local_path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / row["local_path"], target)
                changed = deepcopy(lock)
                if kind == "bytes": (root / lock["files"][0]["local_path"]).write_bytes(b"changed")
                elif kind == "duplicate": changed["files"][1] = deepcopy(changed["files"][0])
                elif kind == "path": changed["files"][0]["source_path"] = "backend/private.py"
                else: changed["source_revision"] = "main"
                target = root / "contracts/hosted-bundle-v1.lock.json"
                target.write_text(json.dumps(changed))
                with self.subTest(kind=kind), self.assertRaises(ContractCheckError): verify(root)
