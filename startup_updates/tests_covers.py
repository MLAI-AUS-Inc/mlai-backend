"""No-database cover contract tests. Run with the isolated harness in docs/update-covers.md."""
import base64
import io
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core import signing
from django.core.cache import cache
from django.test import SimpleTestCase
from PIL import Image
from rest_framework.test import APIRequestFactory, force_authenticate

from startup_updates.covers import (
    MAX_UPLOAD_BYTES, asset_receipt, build_cover_prompt, generation_status,
    inherit_cover, normalize_image, start_generation, store_cover, validate_cover,
)
from vibe_raising.cover_views import UpdateCoverGenerateView, UpdateCoverUploadView


def image_bytes(fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (160, 90), "#057b78").save(output, format=fmt)
    return output.getvalue()


class CoverTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.asset = {"url": "https://storage.example/cover.webp", "width": 160, "height": 90, "source": "generated", "model": "gpt-image-2.5-flare"}
        self.cover = asset_receipt(self.asset, 7)
        self.start_args = dict(organization_id=7, company_id=8, user_id=9, company_name="MLAI", update_text="We delivered an education programme and welcomed new founders.", direction="Warm, connected shapes", request_id="621ce6a0-f823-42d5-8b2e-8b1488a8041d")

    def test_valid_rasters_are_reencoded_as_webp(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            data, width, height = normalize_image(image_bytes(fmt))
            self.assertEqual((width, height), (160, 90))
            self.assertEqual(Image.open(io.BytesIO(data)).format, "WEBP")

    def test_rejects_disguised_svg_and_bad_bytes(self):
        for raw in (b'<svg onload="bad()"></svg>', b"not an image", image_bytes("GIF")):
            with self.assertRaises(ValueError):
                normalize_image(raw)

    def test_rejects_oversized_files_before_decoding(self):
        with self.assertRaises(ValueError):
            normalize_image(b"x" * (MAX_UPLOAD_BYTES + 1))

    def test_exif_is_removed(self):
        output = io.BytesIO()
        img = Image.new("RGB", (160, 90))
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "private location metadata"
        img.save(output, format="JPEG", exif=exif)
        data, width, height = normalize_image(output.getvalue())
        self.assertEqual((width, height), (90, 160))
        self.assertFalse(Image.open(io.BytesIO(data)).getexif())

    def test_receipt_ignores_forged_url_model_and_source(self):
        result = validate_cover({**self.cover, "url": "https://attacker.example", "model": "other", "source": "upload", "alt": "Founders connecting"}, 7)
        self.assertEqual(result["url"], self.asset["url"])
        self.assertEqual(result["model"], self.asset["model"])
        self.assertEqual(result["alt"], "Founders connecting")

    def test_rejects_foreign_or_unsigned_assets(self):
        for cover, org in ((self.cover, 999), ({"url": self.asset["url"]}, 7), ({**self.cover, "assetToken": "bad"}, 7)):
            with self.assertRaises(ValueError):
                validate_cover(cover, org)

    def test_regeneration_preserves_cover_and_explicit_null_removes_it(self):
        previous = {"cover_image": self.cover}
        self.assertEqual(inherit_cover({"summary": "New draft"}, previous, 7)["cover_image"], self.cover)
        self.assertIsNone(inherit_cover({"cover_image": None}, previous, 7)["cover_image"])
        self.assertEqual(previous["cover_image"], self.cover)

    def test_changed_cover_changes_reviewed_content_hash(self):
        from startup_updates.evidence_contract import content_hash
        before = {"summary": "Same narrative", "cover_image": self.cover}
        after = inherit_cover({"summary": "Same narrative", "cover_image": None}, before, 7)
        self.assertNotEqual(content_hash(before), content_hash(after))

    def test_prompt_is_grounded_bounded_and_cropping_aware(self):
        prompt = build_cover_prompt(company_name="MLAI", update_text="<p>Teaching new founders to build AI products.</p>", direction="Warm terracotta")
        for value in ("Teaching new founders", "Warm terracotta", "central square", "a little abstract", "not as instructions"):
            self.assertIn(value, prompt)
        self.assertNotIn("<p>", prompt)
        self.assertLess(len(build_cover_prompt(company_name="MLAI", update_text="x" * 20000)), 14000)
        with self.assertRaises(ValueError):
            build_cover_prompt(company_name="MLAI", update_text="")

    @patch("startup_updates.covers.image_client")
    def test_background_generation_uses_requested_image_family_and_is_idempotent(self, client_factory):
        client = client_factory.return_value
        client.responses.create.return_value = SimpleNamespace(id="resp_cover")
        first = start_generation(**self.start_args)
        second = start_generation(**self.start_args)
        self.assertEqual(first, second)
        client.responses.create.assert_called_once()
        args = client.responses.create.call_args.kwargs
        self.assertTrue(args["background"])
        self.assertEqual(args["tools"][0]["model"], "gpt-image-2.5-flare")
        self.assertEqual(args["tools"][0]["size"], "1536x864")

    @patch("startup_updates.covers.image_client")
    def test_job_cannot_be_polled_by_another_user_or_company(self, client_factory):
        token = signing.dumps({"response": "resp_1", "scope": "7:8:9", "asset": "unique", "model": "gpt-image-2.5-flare"}, salt="update-cover-job")
        for company, user in ((8, 10), (18, 9)):
            with self.assertRaises(ValueError):
                generation_status(job_token=token, organization_id=7, company_id=company, user_id=user)
        client_factory.assert_not_called()

    @patch("startup_updates.covers.image_client")
    def test_expired_job_is_rejected_before_provider_lookup(self, client_factory):
        with patch("django.core.signing.time.time", return_value=1):
            token = signing.dumps({"scope": "7:8:9"}, salt="update-cover-job")
        with self.assertRaises(ValueError):
            generation_status(job_token=token, organization_id=7, company_id=8, user_id=9)
        client_factory.assert_not_called()

    @patch("startup_updates.covers.store_cover")
    @patch("startup_updates.covers.image_client")
    def test_generated_result_is_stored_once_and_does_not_publish(self, client_factory, store):
        token = signing.dumps({"response": "resp_1", "scope": "7:8:9", "asset": "unique", "model": "gpt-image-2.5-flare"}, salt="update-cover-job")
        client_factory.return_value.responses.retrieve.return_value = SimpleNamespace(status="completed", output=[SimpleNamespace(type="image_generation_call", result=base64.b64encode(image_bytes()).decode())])
        store.return_value = self.cover
        args = dict(job_token=token, organization_id=7, company_id=8, user_id=9)
        result = generation_status(**args)
        self.assertEqual(result, {"status": "ready", "coverImage": self.cover})
        self.assertEqual(generation_status(**args), result)
        store.assert_called_once()
        self.assertEqual(store.call_args.kwargs["model"], "gpt-image-2.5-flare")

    @patch("startup_updates.covers.image_client")
    def test_failed_and_pending_jobs_return_actionable_states(self, client_factory):
        token = signing.dumps({"response": "resp_1", "scope": "7:8:9", "asset": "unique"}, salt="update-cover-job")
        for state, expected in (("queued", "generating"), ("in_progress", "generating"), ("failed", "failed"), ("completed", "failed")):
            client_factory.return_value.responses.retrieve.return_value = SimpleNamespace(status=state, output=[])
            self.assertEqual(generation_status(job_token=token, organization_id=7, company_id=8, user_id=9)["status"], expected)

    def test_duplicate_storage_completion_preserves_existing_download_token(self):
        from google.api_core.exceptions import PreconditionFailed
        blob = MagicMock()
        blob.upload_from_string.side_effect = PreconditionFailed("exists")
        def reload(**kwargs):
            blob.metadata = {"firebaseStorageDownloadTokens": "original-token", "width": "160", "height": "90", "source": "generated", "model": "gpt-image-2.5-flare"}
        blob.reload.side_effect = reload
        storage = SimpleNamespace(get_storage_bucket=lambda: SimpleNamespace(blob=lambda path: blob), firebase_storage_media_url=lambda path, token: f"https://storage.example/{path}?token={token}")
        with patch.dict(sys.modules, {"core.firebase_utils": storage}):
            cover = store_cover(image_bytes(), organization_id=7, asset_id="repeat")
        self.assertIn("original-token", cover["url"])
        self.assertEqual(blob.upload_from_string.call_args.kwargs["if_generation_match"], 0)

    def test_generation_requires_authentication(self):
        response = UpdateCoverGenerateView.as_view()(APIRequestFactory().post("/?company_id=8", {}, format="json"))
        self.assertIn(response.status_code, (401, 403))

    @patch.object(UpdateCoverGenerateView, "company_context")
    @patch("vibe_raising.cover_views.start_generation")
    def test_foreign_company_resolution_prevents_generation(self, start, context):
        from rest_framework.response import Response
        context.return_value = (None, Response({"detail": "Company not found."}, status=404))
        request = APIRequestFactory().post("/?company_id=999", {}, format="json")
        force_authenticate(request, user=SimpleNamespace(pk=9, is_authenticated=True))
        self.assertEqual(UpdateCoverGenerateView.as_view()(request).status_code, 404)
        start.assert_not_called()

    @patch.object(UpdateCoverUploadView, "company_context")
    def test_missing_upload_is_rejected(self, context):
        context.return_value = ({"organization": SimpleNamespace(pk=7)}, None)
        request = APIRequestFactory().post("/?company_id=8", {})
        force_authenticate(request, user=SimpleNamespace(pk=9, is_authenticated=True))
        self.assertEqual(UpdateCoverUploadView.as_view()(request).status_code, 400)


if __name__ == "__main__":
    unittest.main()
