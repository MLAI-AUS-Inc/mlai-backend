from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from core.profile_avatars import encode_profile_avatar
from core.views import UpdateProfileView


def avatar_file():
    with Image.new("RGBA", (600, 300), (0, 0, 0, 0)) as image:
        upload = BytesIO()
        image.save(upload, format="PNG")
    return SimpleUploadedFile("avatar.png", upload.getvalue(), content_type="image/png")


class ProfileAvatarUploadTests(SimpleTestCase):
    def test_avatar_is_resized_with_transparency(self):
        output, image_format = encode_profile_avatar(avatar_file())
        self.assertEqual(image_format, "png")
        with Image.open(output) as image:
            self.assertEqual(image.size, (300, 150))
            self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_byte_limit_is_checked_before_decode(self):
        upload = avatar_file()
        with patch("core.profile_avatars.MAX_AVATAR_BYTES", upload.size - 1):
            with patch("core.profile_avatars.Image.open") as open_image:
                with self.assertRaisesRegex(ValueError, "10 MB"):
                    encode_profile_avatar(upload)
        open_image.assert_not_called()

    def test_pixel_limit_is_checked_before_resize(self):
        with patch("core.profile_avatars.MAX_AVATAR_PIXELS", 1000):
            with self.assertRaisesRegex(ValueError, "dimensions"):
                encode_profile_avatar(avatar_file())

    def test_rejected_team_avatar_keeps_profile_and_storage_unchanged(self):
        user = Mock(id=7, email="member@example.test", avatar_url="https://example.test/old")
        team = Mock(id=8, avatar_url="https://example.test/old-team")
        request = SimpleNamespace(user=user, data={"app": "hospital"}, FILES={
            "avatar": avatar_file(),
            "team_avatar": SimpleUploadedFile("invalid.png", b"invalid image"),
        })
        with patch("core.views._active_hospital_team", return_value=team):
            with patch("core.views.store_profile_avatar") as store:
                response = UpdateProfileView().patch(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["field"], "team_avatar")
        self.assertEqual(user.avatar_url, "https://example.test/old")
        self.assertEqual(team.avatar_url, "https://example.test/old-team")
        store.assert_not_called()
        user.save.assert_not_called()
        team.save.assert_not_called()

    def test_failed_storage_returns_retryable_error_without_replacing_avatar(self):
        user = Mock(id=7, email="member@example.test", avatar_url="https://example.test/old")
        request = SimpleNamespace(user=user, data={}, FILES={"avatar": avatar_file()})

        with patch("core.views.store_profile_avatar", side_effect=OSError("storage unavailable")):
            response = UpdateProfileView().patch(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["field"], "avatar")
        self.assertEqual(user.avatar_url, "https://example.test/old")
        user.save.assert_not_called()
