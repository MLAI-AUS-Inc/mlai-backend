"""AuthorPhoto records: the exact bytes' hash travels with the author profile (6 Nov 2026)."""
import hashlib
from io import BytesIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from PIL import Image

from content_factory import author_photos as photos
from content_factory.authors import author_profile_for_renderer, normalize_authors
from content_factory.models import AuthorPhoto, OrganizationContentConfig
from organizations.models import Organization

PREFIX = "https://firebasestorage.googleapis.com/v0/b/test-bucket/o/"


def image_bytes(size=(40, 30)):
    out = BytesIO()
    Image.new("RGB", size, "red").save(out, format="JPEG")
    return out.getvalue()


class Upload:
    def __init__(self, data):
        self.size, self._data = len(data), data

    def read(self, _limit):
        return self._data


class AuthorPhotoRecordTests(TestCase):
    def setUp(self):
        self.enterContext(patch.object(photos, "_storage_url_prefix", return_value=PREFIX))
        self.org = Organization.objects.create(name="Tala", domain="tala.test")
        self.other = Organization.objects.create(name="Other", domain="other.test")

    def upload(self, org=None):
        stored = {}

        def fake_upload(file_obj, destination, content_type=None):
            stored["bytes"] = file_obj.getvalue()
            return PREFIX + destination.replace("/", "%2F") + "?alt=media&token=t"

        with patch("core.firebase_utils.upload_file_to_storage", side_effect=fake_upload):
            url = photos.upload_photo(Upload(image_bytes()), company_id="c1",
                                      organization_id=(org or self.org).pk)
        return url, stored["bytes"]

    def test_upload_records_the_exact_stored_bytes(self):
        url, data = self.upload()
        record = AuthorPhoto.objects.get(url=url)
        self.assertEqual(record.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual((record.organization_id, record.source, record.width, record.height),
                         (self.org.pk, "upload", 40, 30))
        self.assertTrue(record.storage_path.startswith("author-photos/c1/"))
        self.assertEqual(photos.storage_path_for(url), record.storage_path)

    def test_saved_author_gets_its_record_and_client_values_are_ignored(self):
        url, _ = self.upload()
        record = AuthorPhoto.objects.get(url=url)
        submitted = normalize_authors([{"name": "Sonia", "avatarUrl": url, "avatarPhotoId": "forged",
                                        "avatarSha256": "0" * 64}])
        [author] = photos.store_author_photos(submitted, previous=[], company_id="c1", organization_id=self.org.pk)
        self.assertEqual((author["avatarPhotoId"], author["avatarSha256"]), (str(record.pk), record.sha256))
        profile = author_profile_for_renderer(normalize_authors([author])[0])
        self.assertEqual((profile["avatarPhotoId"], profile["avatarSha256"]), (str(record.pk), record.sha256))

    def test_another_organizations_photo_is_not_linked(self):
        url, _ = self.upload(org=self.other)
        [author] = photos.store_author_photos(normalize_authors([{"name": "Sonia", "avatarUrl": url,
            "avatarPhotoId": "x", "avatarSha256": "0" * 64}]), previous=[], company_id="c1", organization_id=self.org.pk)
        self.assertNotIn("avatarPhotoId", author)
        self.assertNotIn("avatarSha256", author)

    def test_backfill_records_legacy_stored_photos_once(self):
        url = PREFIX + "1713648181724.jpeg?alt=media&token=t"
        data = image_bytes()
        OrganizationContentConfig.objects.create(organization=self.org, authors=[{"id": "sonia", "name": "Sonia",
                                                                                   "avatarUrl": url}])
        with patch("content_factory.management.commands.record_author_photos.fetch_photo", return_value=data) as fetch:
            call_command("record_author_photos", "--apply", stdout=BytesIOText(), stderr=BytesIOText())
            call_command("record_author_photos", "--apply", stdout=BytesIOText(), stderr=BytesIOText())
        self.assertEqual(fetch.call_count, 1)
        record = AuthorPhoto.objects.get(url=url)
        self.assertEqual((record.source, record.sha256, record.storage_path),
                         ("legacy", hashlib.sha256(data).hexdigest(), "1713648181724.jpeg"))
        [author] = OrganizationContentConfig.objects.get(organization=self.org).authors
        self.assertEqual(author["avatarPhotoId"], str(record.pk))

    def test_backfill_dry_run_changes_nothing(self):
        url = PREFIX + "legacy.jpeg?alt=media"
        OrganizationContentConfig.objects.create(organization=self.org, authors=[{"name": "Sonia", "avatarUrl": url}])
        with patch("content_factory.management.commands.record_author_photos.fetch_photo") as fetch:
            call_command("record_author_photos", stdout=BytesIOText(), stderr=BytesIOText())
        fetch.assert_not_called()
        self.assertFalse(AuthorPhoto.objects.exists())


class BytesIOText:
    def write(self, *_args, **_kwargs):
        pass

    def flush(self):
        pass
