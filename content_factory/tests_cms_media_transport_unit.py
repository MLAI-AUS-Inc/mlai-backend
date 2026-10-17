"""Image uploads share the pinned credential boundary and never certify live bytes."""

from copy import deepcopy
from io import BytesIO
import json
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlsplit

from django.test import SimpleTestCase
from PIL import Image

from . import cms_api_transport as api
from . import cms_media_transport as media
from .cms_connection_contract import CmsCredential
from . import tests_cms_api_transport_unit as transport_fixtures


def raster(format="PNG"):
    output = BytesIO()
    Image.new("RGB", (3, 2), (17, 51, 153)).save(output, format=format)
    return output.getvalue()


class CmsMediaTransportTests(SimpleTestCase):
    def setUp(self):
        transport_fixtures.CmsApiTransportTests.setUp(self)
        self.image = media.prepare_image(raster())
        self.upload_id = str(uuid4())

    def result(self, **changes):
        return {"id": 42, "type": "attachment", "media_type": "image", "mime_type": "image/png",
            "slug": media._identity(self.upload_id, self.image),
            "description": {"raw": media._marker(self.upload_id, self.image)},
            "source_url": self.binding["public_origin"] + "wp-content/uploads/owned.png", **changes}

    def upload(self, **changes):
        return media.upload_image(self.binding, self.credential, **{"upload_id": self.upload_id, "image": self.image,
            "title": "Owned <image>", "alt_text": "A synthetic image", "caption": "A caption & attribution", **changes})

    def test_only_bounded_decodable_static_rasters_are_accepted(self):
        for format, expected in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            value = media.prepare_image(raster(format))
            self.assertEqual((value.media_type, value.width, value.height), (expected, 3, 2))
            self.assertNotIn(str(value.raw), repr(value))
        for raw in (b"", b"<svg><script>private()</script></svg>", b"%PDF-1.7", raster()[:24], bytearray(raster()), b"x" * (media.MAX_IMAGE_BYTES + 1)):
            with self.subTest(kind=type(raw), length=len(raw)), self.assertRaises(api.CmsApiError): media.prepare_image(raw)
        output = BytesIO()
        Image.new("RGB", (4097, 1)).save(output, format="PNG")
        with self.assertRaises(api.CmsApiError): media.prepare_image(output.getvalue())

    def test_animated_webp_cannot_enter_a_static_owned_media_slot(self):
        output = BytesIO()
        Image.new("RGB", (3, 2), "red").save(output, format="WEBP", save_all=True,
            append_images=[Image.new("RGB", (3, 2), "blue")], duration=20, loop=0)
        with self.assertRaises(api.CmsApiError): media.prepare_image(output.getvalue())

    def test_wordpress_multipart_keeps_exact_raster_and_private_operation_reference(self):
        self.response.read.return_value = json.dumps(self.result()).encode()
        result = self.upload()
        self.assertEqual((result.provider_id, result.source_sha256, result.public_bytes_verified), (42, self.image.sha256, False))
        method, target = self.client.request.call_args.args
        self.assertEqual((method, target), ("POST", "/wordpress/wp-json/wp/v2/media?context=edit"))
        sent = self.client.request.call_args.kwargs
        self.assertTrue(sent["headers"]["Content-Type"].startswith("multipart/form-data; boundary=mlai-image-"))
        self.assertIn(self.image.raw, sent["body"])
        for field in (b'name="file"', b'name="slug"', b'name="alt_text"', b'name="description"', self.image.sha256.encode(), b"Owned &lt;image&gt;"):
            self.assertIn(field, sent["body"])
        self.assertNotIn(b"synthetic-application-password", sent["body"])
        self.assertNotIn("Cookie", sent["headers"])
        self.assertNotIn(result.url, repr(result))
        self.response.close.assert_called_once()
        self.client.close.assert_called_once()

    def test_ghost_upload_uses_selected_admin_origin_and_exact_echoed_reference(self):
        self.binding = {**self.binding, "platform": "ghost", "api_origin": "https://admin.fixture.test/"}
        self.credential = CmsCredential(platform="ghost", secret="a" * 24 + ":" + "b" * 64)
        url = self.binding["public_origin"] + "content/images/owned.png"
        self.response.read.return_value = json.dumps({"images": [{"url": url, "ref": media._marker(self.upload_id, self.image)}]}).encode()
        result = self.upload()
        self.assertEqual((result.platform, result.provider_id, result.url, result.public_bytes_verified), ("ghost", None, url, False))
        self.pin.assert_called_once_with("https://admin.fixture.test/ghost/api/admin/images/upload/", "admin.fixture.test")
        sent = self.client.request.call_args.kwargs
        self.assertEqual(self.client.request.call_args.args, ("POST", "/ghost/api/admin/images/upload/"))
        self.assertEqual(sent["headers"]["Accept-Version"], "v6.0")
        self.assertIn(b'name="purpose"\r\n\r\nimage\r\n', sent["body"])
        self.assertIn(b'name="ref"', sent["body"])
        self.assertNotIn(b'name="alt_text"', sent["body"])

    def test_malformed_images_credentials_ids_and_header_text_fail_before_any_http(self):
        forged = media.CmsImage(self.image.raw, "f" * 64, "image/png", "png", 3, 2)
        for changes in ({"upload_id": "filename\r\nInjected: private"}, {"image": forged}, {"image": b"private"},
                {"title": "x" * 201}, {"alt_text": "bad\r\nheader"}, {"caption": "x" * 2001}):
            with self.subTest(fields=tuple(changes)), self.assertRaises(api.CmsApiError): self.upload(**changes)
        self.credential = CmsCredential(platform="ghost", secret="a" * 24 + ":" + "b" * 64)
        with self.assertRaises(api.CmsApiError): self.upload()
        self.pin.assert_not_called()
        self.client.request.assert_not_called()

    def test_upload_never_follows_redirects_or_retries_an_unknown_effect(self):
        for status in (302, 307, 503):
            self.response.status = status
            self.headers["Location"] = "https://foreign.test/collect"
            with self.subTest(status=status), self.assertRaises(api.CmsApiError): self.upload()
        self.assertEqual(self.client.request.call_count, 3)
        self.response.read.assert_not_called()

    def test_private_dns_and_oversized_or_non_json_results_keep_existing_transport_limits(self):
        from .website_contract import WebsiteAuthorityError
        self.pin.side_effect = WebsiteAuthorityError("deployment_origin_invalid", "Private address")
        with self.assertRaises(api.CmsApiError): self.upload()
        self.connect.assert_not_called()
        self.pin.side_effect = lambda url, domain: (urlsplit(url), ["8.8.8.8"])
        for headers, body in (({"Content-Type": "text/html"}, b"private provider body"),
                ({"Content-Type": "application/json"}, b"x" * (api.MAX_JSON_BYTES + 1))):
            self.headers.update(headers)
            self.response.read.return_value = body
            with self.subTest(headers=headers), self.assertRaises(api.CmsApiError) as error: self.upload()
            self.assertNotIn("private provider body", str(error.exception))

    def test_wordpress_receipt_requires_exact_native_identity_and_source_marker(self):
        for changes in ({"id": True}, {"mime_type": "text/html"}, {"media_type": "file"}, {"type": "post"},
                {"slug": "unrelated"}, {"description": {"raw": "other operation"}}, {"description": []}):
            with self.subTest(fields=tuple(changes)), self.assertRaises(api.CmsApiError):
                media.validate_uploaded_image(self.result(**changes), binding=self.binding, upload_id=self.upload_id, image=self.image)

    def test_acknowledgement_does_not_authorize_external_storage_or_url_traversal(self):
        for url in ("https://foreign.test/owned.png", "http://fixture.test/owned.png", "https://user:secret@fixture.test/a.png",
                "https://fixture.test/wordpress/../private.png", "https://fixture.test/wordpress/%2e%2e/private.png",
                "https://fixture.test/wordpress/%2fprivate.png", "https://fixture.test/wordpress/a.png?token=private",
                "https://fixture.test/wordpress/a.png#fragment", "https://fixture.test:444/wordpress/a.png"):
            with self.subTest(url=url), self.assertRaises(api.CmsApiError):
                media.validate_uploaded_image(self.result(source_url=url), binding=self.binding, upload_id=self.upload_id, image=self.image)

    def test_ghost_cannot_acknowledge_a_different_missing_or_multiple_upload_reference(self):
        binding = {**self.binding, "platform": "ghost", "api_origin": "https://admin.fixture.test/"}
        row = {"url": binding["public_origin"] + "content/images/owned.png", "ref": media._marker(self.upload_id, self.image)}
        for value in ({"images": [{**row, "ref": "other"}]}, {"images": []}, {"images": [row, deepcopy(row)]}, {"images": [None]}, {}):
            with self.subTest(value=value), self.assertRaises(api.CmsApiError):
                media.validate_uploaded_image(value, binding=binding, upload_id=self.upload_id, image=self.image)
