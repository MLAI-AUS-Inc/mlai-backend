"""Author photo copying with fake DNS, HTTP and storage boundaries."""
import base64
from io import BytesIO
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.urls import resolve
from PIL import Image
from rest_framework.response import Response

from agent_runtime.webhook_transport import CallbackEndpointError
from content_factory import author_photos as photos
from content_factory import vibe_marketing_views as views

LINKEDIN_PHOTO = ("https://media.licdn.com/dms/image/v2/C5603AQ/profile-displayphoto-shrink_200_200/"
                  "0/1579726625398?e=2147483647&v=beta&t=signature")
STORED = ("https://firebasestorage.googleapis.com/v0/b/mlai-main-website.firebasestorage.app/o/"
          "author-photos%2Fcompany%2Fphoto.jpg?alt=media&token=t")


def image_bytes(fmt="JPEG", mode="RGB", size=(1200, 900)):
    output = BytesIO()
    Image.new(mode, size, (200, 40, 40, 128) if mode == "RGBA" else (200, 40, 40)).save(output, format=fmt)
    return output.getvalue()


class NeedsCopyTests(SimpleTestCase):
    def test_only_external_links_are_copied(self):
        self.assertTrue(photos.needs_copy(LINKEDIN_PHOTO))
        self.assertTrue(photos.needs_copy("http://example.com/me.jpg"))
        self.assertTrue(photos.needs_copy(
            "https://firebasestorage.googleapis.com/v0/b/another-bucket/o/me.jpg"))
        for kept in (STORED, "/images/team/sam.jpg", "", None, "data:image/png;base64,AAAA"):
            with self.subTest(kept=kept):
                self.assertFalse(photos.needs_copy(kept))


class FetchPhotoTests(SimpleTestCase):
    def fetch(self, url, *responses, resolver=None):
        with patch.object(photos, "resolve_callback",
                          side_effect=resolver or (lambda url, **_: ("media.licdn.com", "13.107.42.14", "/p"))) as dns, \
             patch.object(photos, "_get", side_effect=list(responses)) as get:
            try:
                return photos.fetch_photo(url), dns, get
            except photos.AuthorPhotoError as exc:
                return exc, dns, get

    def test_follows_redirects_and_repins_each_hop(self):
        body, dns, _get = self.fetch("http://example.com/me.jpg",
                                     (302, "https://cdn.example.com/me.jpg", b""), (200, None, b"jpeg"))
        self.assertEqual(body, b"jpeg")
        self.assertEqual([call.args[0] for call in dns.call_args_list],
                         ["https://example.com/me.jpg", "https://cdn.example.com/me.jpg"])

    def test_linkedin_profile_page_is_explained_without_fetching(self):
        error, dns, _get = self.fetch("https://www.linkedin.com/in/gisele/overlay/photo/")
        self.assertEqual(error.code, "author_photo_not_image")
        self.assertIn("Copy image address", str(error))
        dns.assert_not_called()

    def test_private_destination_is_rejected(self):
        def private(url, **_):
            raise CallbackEndpointError("private_destination")
        error, _dns, get = self.fetch("https://metadata.internal/latest", resolver=private)
        self.assertEqual(error.code, "author_photo_private")
        get.assert_not_called()

    def test_expired_linkedin_link_is_explained(self):
        error, _dns, _get = self.fetch(LINKEDIN_PHOTO, (403, None, b""))
        self.assertEqual(error.code, "author_photo_unavailable")
        self.assertIn("expired", str(error))

    def test_redirect_loops_stop(self):
        loop = [(302, "https://example.com/again", b"")] * (photos.MAX_REDIRECTS + 1)
        error, _dns, _get = self.fetch("https://example.com/start", *loop)
        self.assertEqual(error.code, "author_photo_unreachable")

    def test_response_body_is_bounded(self):
        response = Mock(status=200, getheader=lambda name, default=None: None)
        response.read.return_value = b"x" * (photos.MAX_PHOTO_BYTES + 1)
        connection = Mock(sock=None)
        connection.getresponse.return_value = response
        with patch.object(photos.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaises(photos.AuthorPhotoError) as raised:
                photos._get("media.licdn.com", "13.107.42.14", "/p", photos.time.monotonic() + 5)
        self.assertEqual(raised.exception.code, "author_photo_too_large")
        response.read.assert_called_once_with(photos.MAX_PHOTO_BYTES + 1)
        connection.close.assert_called()


class EncodePhotoTests(SimpleTestCase):
    def test_photo_is_downscaled_to_jpeg(self):
        data, content_type = photos.encode_photo(image_bytes(), photos.STORED_PHOTO_SIZE)
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(Image.open(BytesIO(data)).size, (800, 600))

    def test_transparent_photo_stays_png(self):
        data, content_type = photos.encode_photo(image_bytes("PNG", "RGBA", (300, 300)), 256)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(Image.open(BytesIO(data)).mode, "RGBA")

    def test_non_images_are_rejected(self):
        for data in (b"<html>LinkedIn sign in</html>", image_bytes("GIF", "RGB", (10, 10))):
            with self.subTest(data=data[:10]):
                with self.assertRaises(photos.AuthorPhotoError) as raised:
                    photos.encode_photo(data, 256)
                self.assertEqual(raised.exception.code, "author_photo_not_image")

    def test_preview_is_a_small_data_url(self):
        with patch.object(photos, "fetch_photo", return_value=image_bytes()):
            preview = photos.preview_photo(LINKEDIN_PHOTO)
        header, encoded = preview.split(",", 1)
        self.assertEqual(header, "data:image/jpeg;base64")
        self.assertEqual(Image.open(BytesIO(base64.b64decode(encoded))).size, (256, 192))


class CopyPhotoTests(SimpleTestCase):
    def test_copy_stores_under_company_prefix(self):
        with patch.object(photos, "fetch_photo", return_value=image_bytes()), \
             patch("core.firebase_utils.upload_file_to_storage", return_value=STORED) as upload:
            self.assertEqual(photos.copy_photo(LINKEDIN_PHOTO, company_id="company"), STORED)
        destination = upload.call_args.args[1]
        self.assertRegex(destination, r"^author-photos/company/[0-9a-f]{32}\.jpg$")
        self.assertEqual(upload.call_args.kwargs["content_type"], "image/jpeg")

    def test_storage_failure_is_retryable(self):
        with patch.object(photos, "fetch_photo", return_value=image_bytes()), \
             patch("core.firebase_utils.upload_file_to_storage", return_value=None):
            with self.assertRaises(photos.AuthorPhotoError) as raised:
                photos.copy_photo(LINKEDIN_PHOTO, company_id="company")
        self.assertEqual((raised.exception.code, raised.exception.status), ("author_photo_storage_failed", 502))


class StoreAuthorPhotosTests(SimpleTestCase):
    def author(self, author_id, url, name="Gisele Example"):
        return {"id": author_id, "name": name, "avatarUrl": url}

    def test_new_links_are_replaced_once_and_kept_photos_untouched(self):
        authors = [self.author("a", LINKEDIN_PHOTO), self.author("b", LINKEDIN_PHOTO, "Sam"),
                   self.author("c", STORED), self.author("d", "/team/d.jpg"), self.author("e", "")]
        with patch.object(photos, "copy_photo", return_value=STORED) as copy:
            stored = photos.store_author_photos(authors, previous=[], company_id="company")
        copy.assert_called_once_with(LINKEDIN_PHOTO, company_id="company")
        self.assertEqual([author["avatarUrl"] for author in stored],
                         [STORED, STORED, STORED, "/team/d.jpg", ""])
        self.assertEqual(authors[0]["avatarUrl"], LINKEDIN_PHOTO)

    def test_failed_new_link_rejects_the_save_with_the_author(self):
        failure = photos.AuthorPhotoError("author_photo_unavailable", "This photo link has expired.")
        with patch.object(photos, "copy_photo", side_effect=failure):
            with self.assertRaises(photos.AuthorPhotoError) as raised:
                photos.store_author_photos([self.author("a", LINKEDIN_PHOTO)], previous=[], company_id="c")
        self.assertEqual(raised.exception.author_id, "a")
        self.assertEqual(str(raised.exception),
                         "Gisele Example's photo couldn't be copied. This photo link has expired.")

    def test_failed_previously_saved_link_does_not_block_other_edits(self):
        failure = photos.AuthorPhotoError("author_photo_unavailable", "This photo link has expired.")
        with patch.object(photos, "copy_photo", side_effect=failure):
            stored = photos.store_author_photos([self.author("a", LINKEDIN_PHOTO)],
                                                previous=[self.author("a", LINKEDIN_PHOTO)], company_id="c")
        self.assertEqual(stored[0]["avatarUrl"], LINKEDIN_PHOTO)


class SettingsViewTests(SimpleTestCase):
    def setUp(self):
        self.company = Obj(id="company", organization_id=3)
        patcher = patch.object(views, "_resolve_profile_company_or_response",
                               return_value=(Obj(), self.company, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(views.OrganizationContentConfig.objects, "filter")
        self.configs = patcher.start()
        self.addCleanup(patcher.stop)
        self.configs.return_value.values_list.return_value.first.return_value = []
        patcher = patch("founder_tools.profile_fields.validate_profile_fields")
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, store):
        request = Obj(data={"companyId": "company", "authors": [{"id": "a", "name": "Gisele",
                                                                 "avatarUrl": LINKEDIN_PHOTO}]},
                      query_params={}, user=Obj(id=7))
        view = views.VibeMarketingSettingsView()
        seen = {}

        def save(request):
            seen["authors"] = view.prepared_authors
            return Response({"saved": True})

        with patch("content_factory.author_photos.store_author_photos", side_effect=store) as stored, \
             patch.object(view, "_save_settings", side_effect=save):
            return view.put(request), seen, stored

    def test_photos_are_copied_before_settings_are_saved(self):
        response, seen, stored = self.put(lambda authors, **_: [{**authors[0], "avatarUrl": STORED}])
        self.assertEqual(response.data, {"saved": True})
        self.assertEqual(seen["authors"][0]["avatarUrl"], STORED)
        self.assertEqual(stored.call_args.kwargs["company_id"], "company")
        self.configs.assert_called_once_with(organization_id=3)

    def test_photo_failure_returns_a_field_error_without_saving(self):
        def fail(authors, **_):
            raise photos.AuthorPhotoError("author_photo_unavailable", "Gisele's photo couldn't be copied.",
                                          author_id="a")
        response, seen, _stored = self.put(fail)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"detail": "Gisele's photo couldn't be copied.",
                                         "code": "author_photo_unavailable", "field": "authors", "authorId": "a"})
        self.assertEqual(seen, {})


class PreviewViewTests(SimpleTestCase):
    def post(self, url, **patches):
        request = Obj(data={"companyId": "company", "url": url}, query_params={}, user=Obj(id=7))
        with patch.object(views, "_resolve_profile_company_or_response", return_value=(Obj(), Obj(id="company"), None)), \
             patch("content_factory.author_photos.preview_photo", **patches) as preview:
            return views.VibeMarketingAuthorPhotoPreviewView().post(request), preview

    def test_preview_returns_a_data_url(self):
        response, preview = self.post(LINKEDIN_PHOTO, return_value="data:image/jpeg;base64,AAAA")
        self.assertEqual((response.status_code, response.data), (200, {"previewUrl": "data:image/jpeg;base64,AAAA"}))
        preview.assert_called_once_with(LINKEDIN_PHOTO)

    def test_preview_rejects_non_links_and_reports_fetch_errors(self):
        response, preview = self.post("not a link")
        self.assertEqual(response.status_code, 400)
        preview.assert_not_called()
        response, _preview = self.post(LINKEDIN_PHOTO, side_effect=photos.AuthorPhotoError(
            "author_photo_unavailable", "This photo link has expired."))
        self.assertEqual((response.status_code, response.data["code"]), (400, "author_photo_unavailable"))

    def test_chat_facade_exposes_the_preview(self):
        from community_chat import my_startup_views as facade
        for slash in ("", "/"):
            match = resolve("/api/v1/my-startup/vibe-marketing/authors/photo-preview" + slash)
            self.assertIs(match.func.view_class, facade.AuthorPhotoPreviewView)
