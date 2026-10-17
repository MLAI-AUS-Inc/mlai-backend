"""Owned images require fresh exact public bytes without forwarding credentials."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import uuid4

from django.test import SimpleTestCase
from PIL import Image

from . import cms_media_evidence as evidence
from .cms_api_transport import CmsApiError
from .cms_media_transport import UploadedImage
from .tests_cms_inventory_contract_unit import binding


def raster(color="blue"):
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


class CmsMediaEvidenceTests(SimpleTestCase):
    def setUp(self):
        self.binding = binding()
        self.raw = raster()
        self.image = evidence.prepare_image(self.raw)
        self.asset = {"component_id": "hero-image", "src": "https://firebasestorage.googleapis.com/v0/b/fixture/o/hero.png?alt=media&token=synthetic-download-token",
            "alt": "A blue synthetic image", "sha256": self.image.sha256, "width": 32, "height": 24}
        self.ack = UploadedImage("wordpress", 42, str(uuid4()), self.image.sha256,
            self.binding["public_origin"] + "wp-content/uploads/owned.png")
        self.headers = {"Content-Type": "image/png"}
        self.response = Mock(status=200)
        self.response.getheader.side_effect = lambda name, default=None: self.headers.get(name, default)
        self.response.read.return_value = self.raw
        self.client = Mock()
        self.client.getresponse.return_value = self.response
        self.addresses = patch.object(evidence, "public_addresses", side_effect=lambda url, host: (urlsplit(url), ["203.0.113.10"]))
        self.pinned = patch.object(evidence, "PinnedHTTPSConnection", return_value=self.client)
        self.dns = self.addresses.start(); self.pinned.start()
        self.addCleanup(self.addresses.stop); self.addCleanup(self.pinned.stop)
        self.now = datetime.now(timezone.utc)

    def observe(self):
        return evidence.observe_owned_image(self.binding, handoff_digest="a" * 64, asset=self.asset,
            acknowledgement=self.ack, checked_at=self.now)

    def test_exact_source_bytes_and_dimensions_are_read_once_without_a_service_or_cms_key(self):
        self.assertEqual(evidence.fetch_source_image(self.asset), self.image)
        self.dns.assert_called_once_with(self.asset["src"].split("?", 1)[0], "firebasestorage.googleapis.com")
        method, target = self.client.request.call_args.args
        self.assertEqual(method, "GET")
        self.assertEqual(target, "/v0/b/fixture/o/hero.png?alt=media&token=synthetic-download-token")
        headers = self.client.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Host"], "firebasestorage.googleapis.com")
        self.assertFalse(set(headers) & {"Authorization", "Cookie", "X-API-KEY", "Referer"})
        self.response.close.assert_called_once(); self.client.close.assert_called_once()

    def test_acknowledgement_becomes_only_an_exact_public_image_observation(self):
        value = self.observe()
        self.assertEqual(value["kind"], "cms_public_image_observation")
        self.assertEqual(value["status"], "bytes_verified")
        self.assertEqual(value["public_sha256"], self.asset["sha256"])
        self.assertEqual(value["source_sha256"], self.asset["sha256"])
        self.assertEqual(value["public_url"], self.ack.url)
        self.assertNotIn("synthetic-download-token", str(value))
        self.assertNotIn("approved", value)
        self.assertEqual(evidence.validate_owned_observation(value, binding=self.binding, handoff_digest="a" * 64,
            asset=self.asset, acknowledgement=self.ack, now=self.now), value)
        self.assertFalse(set(self.client.request.call_args.kwargs["headers"]) & {"Authorization", "Cookie", "X-API-KEY"})

    def test_no_redirect_or_failed_http_reply_can_be_treated_as_public_image_delivery(self):
        for status, category in ((301, "invalid_image"), (302, "invalid_image"), (404, "invalid_image"), (429, "transient"), (503, "transient")):
            self.response.status = status
            with self.subTest(status=status), self.assertRaises(CmsApiError) as error: self.observe()
            self.assertEqual(error.exception.category, category)
        self.assertEqual(self.client.request.call_count, 5)

    def test_mime_encoding_size_or_invalid_decoded_raster_is_rejected_and_closed(self):
        for header in ({"Content-Type": "image/svg+xml"}, {"Content-Encoding": "gzip"}, {"Content-Length": "99999999"},
                {"Content-Length": "-1"}, {"Content-Type": "image/jpeg"}):
            self.headers = {"Content-Type": "image/png", **header}
            with self.subTest(header=header), self.assertRaises(CmsApiError): self.observe()
        self.headers = {"Content-Type": "image/png"}
        for content in (b"<svg>synthetic-private-body</svg>", b"x" * (evidence.MAX_IMAGE_BYTES + 1), raster("red")):
            self.response.read.return_value = content
            with self.assertRaises(CmsApiError) as error: self.observe()
            self.assertNotIn("synthetic-private-body", str(error.exception))

    def test_changed_generated_hash_or_dimensions_are_not_silently_resized_or_relabelled(self):
        for changes in ({"sha256": "f" * 64}, {"width": 33}, {"height": 25}, {"width": True}, {"width": 4097}):
            with self.subTest(changes=changes), self.assertRaises(CmsApiError):
                evidence.fetch_source_image({**self.asset, **changes})

    def test_foreign_storage_credentials_or_owned_media_origin_are_denied_before_dns(self):
        for url in ("http://firebasestorage.googleapis.com/v0/b/fixture/o/hero.png", "https://private.test/hero.png",
                "https://user:private@firebasestorage.googleapis.com/v0/b/fixture/o/hero.png", self.asset["src"] + "#fragment"):
            with self.subTest(url=url), self.assertRaises(CmsApiError): evidence.fetch_source_image({**self.asset, "src": url})
        for url in ("https://foreign.test/owned.png", "https://user:private@fixture.test/owned.png", self.ack.url + "?key=private"):
            changed = UploadedImage("wordpress", 42, self.ack.upload_id, self.image.sha256, url)
            with self.subTest(url=url), self.assertRaises(CmsApiError):
                evidence.observe_owned_image(self.binding, handoff_digest="a" * 64, asset=self.asset, acknowledgement=changed)
        self.dns.assert_not_called()

    def test_stale_rechecksummed_evidence_wrong_slot_scope_or_boolean_ids_cannot_pass(self):
        value = self.observe()
        for changes in ({"schema_version": True}, {"provider_id": True}, {"scope_sha256": "f" * 64},
                {"component_id": "other-slot"}, {"public_sha256": "f" * 64}, {"public_url": "https://foreign.test/owned.png"},
                {"checked_at": (self.now - timedelta(minutes=16)).isoformat()},
                {"checked_at": (self.now + timedelta(minutes=1)).isoformat()}, {"approved": True}):
            changed = {**deepcopy(value), **changes}
            changed["digest"] = evidence.digest({key: item for key, item in changed.items() if key != "digest"})
            with self.subTest(changes=changes), self.assertRaises(CmsApiError):
                evidence.validate_owned_observation(changed, binding=self.binding, handoff_digest="a" * 64,
                    asset=self.asset, acknowledgement=self.ack, now=self.now)

    def test_ghost_image_proof_uses_the_same_public_bytes_boundary_without_fake_media_id(self):
        self.binding = binding("ghost")
        self.ack = UploadedImage("ghost", None, str(uuid4()), self.image.sha256,
            self.binding["public_origin"] + "content/images/owned.png")
        value = self.observe()
        self.assertIsNone(value["provider_id"])
        self.assertEqual(evidence.validate_owned_observation(value, binding=self.binding, handoff_digest="a" * 64,
            asset=self.asset, acknowledgement=self.ack, now=self.now), value)
