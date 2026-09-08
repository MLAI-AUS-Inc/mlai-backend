"""Canonical profile API regressions; run only with migration approval."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient

from community_chat.account_profiles import (
    ProfileVersionConflict,
    update_account_profile,
)
from community_chat.account_sessions import (
    issue_account_session,
    rotate_account_session,
)
from community_chat.models import CommunityChatAccountSession
from community_chat.serializers import profile_version_for_user

ORIGIN = "https://chat.mlai.au"


def credentials_for(user, *, web=False):
    return issue_account_session(
        user,
        SimpleNamespace(
            client_id="mlai-chat-web" if web else "mlai-chat-ios",
            installation_id=uuid.uuid4(),
            origin=ORIGIN if web else "mlaichat://callback",
            platform="web" if web else "ios",
            device_name="Profile test",
            public_key="a" * 64,
        ),
    )


@override_settings(COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, "mlaichat://callback"])
class AccountProfileTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="profile@example.com",
            first_name="Alex",
            last_name="Morgan",
            avatar_url="https://media.example.com/original.png",
            about="Original bio",
        )
        self.credentials = credentials_for(self.user)
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.credentials.access_token}"
        )
        self.url = reverse("community_chat_account")
        self.version = profile_version_for_user(self.user)

    def patch_profile(self, **changes):
        return self.client.patch(
            self.url, {"profile_version": self.version, **changes}, format="json"
        )

    def test_patch_persists_and_returns_full_account_without_private_public_fields(
        self,
    ):
        response = self.patch_profile(display_name="  New Name  ", about="  New bio  ")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "New Name")
        self.assertEqual(self.user.about, "New bio")
        self.assertEqual(self.user.avatar_url, "https://media.example.com/original.png")
        self.assertEqual(response.data["profile"]["display_name"], "New Name")
        self.assertNotEqual(response.data["profile"]["profile_version"], self.version)
        self.assertEqual(
            response.data["session"]["id"], str(self.credentials.session.id)
        )
        self.assertIn("devices", response.data)
        self.assertEqual(response.data["session"]["public_key"], "a" * 64)
        for field in ("email", "id", "first_name", "last_name", "password"):
            self.assertNotIn(field, response.data["public_profile"])
        self.assertEqual(
            self.client.get(self.url).data["profile"], response.data["profile"]
        )

    def test_partial_avatar_and_removal_preserve_name_and_bio(self):
        response = self.patch_profile(avatar_url="https://media.example.com/new.png")
        self.assertEqual(response.status_code, 200)
        self.version = response.data["profile"]["profile_version"]
        response = self.patch_profile(avatar_url=None, about="")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.avatar_url)
        self.assertEqual(self.user.about, "")
        self.assertEqual(self.user.full_name, "Alex Morgan")

    def test_stale_version_cannot_overwrite_a_newer_edit(self):
        self.assertEqual(self.patch_profile(display_name="First edit").status_code, 200)
        response = self.patch_profile(display_name="Stale edit")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "profile_version_conflict")
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "First edit")

    def test_other_accounts_are_unchanged_and_privileged_fields_are_rejected(self):
        other = get_user_model().objects.create_user(
            email="other@example.com", first_name="Other"
        )
        for fields in (
            {"id": str(other.pk)},
            {"email": other.email},
            {"is_staff": True},
            {"password": "forbidden"},
            {"first_name": "Bypass"},
            {"auth_version": 100},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(
                    self.patch_profile(display_name="No write", **fields).status_code,
                    400,
                )
        self.user.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.user.full_name, "Alex Morgan")
        self.assertEqual(other.first_name, "Other")
        self.assertFalse(self.user.is_staff)

    def test_validation_fails_before_writing(self):
        for fields in (
            {},
            {"display_name": " "},
            {"display_name": "x" * 81},
            {"about": "x" * 501},
            {"about": None},
            {"avatar_url": "data:image/svg+xml,<svg/>"},
            {"avatar_url": "ftp://media.example.com/image.png"},
            {"avatar_url": "https://user:secret@media.example.com/image.png"},
            {"avatar_url": "https://media.example.com/" + "x" * 201},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(self.patch_profile(**fields).status_code, 400)
        response = self.client.patch(
            self.url, {"display_name": "Missing version"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(profile_version_for_user(self.user), self.version)

    def test_version_advances_past_a_future_existing_timestamp(self):
        future = timezone.now() + timedelta(minutes=1)
        get_user_model().objects.filter(pk=self.user.pk).update(updated_at=future)
        self.version = future.isoformat()
        self.assertEqual(self.patch_profile(display_name="New name").status_code, 200)
        self.user.refresh_from_db()
        self.assertGreater(self.user.updated_at, future)

    def test_cookie_writes_require_the_session_origin(self):
        credentials = credentials_for(self.user, web=True)
        client = APIClient()
        client.cookies["mlai_chat_access"] = credentials.access_token
        body = {"display_name": "Browser name", "profile_version": self.version}
        for origin in (None, "https://untrusted.example.com"):
            headers = {"HTTP_ORIGIN": origin} if origin else {}
            self.assertEqual(
                client.patch(self.url, body, format="json", **headers).status_code, 401
            )
        self.assertEqual(
            client.patch(self.url, body, format="json", HTTP_ORIGIN=ORIGIN).status_code,
            200,
        )

    def test_revocation_between_authentication_and_write_is_rechecked(self):
        CommunityChatAccountSession.objects.filter(
            pk=self.credentials.session.pk
        ).update(revoked_at=timezone.now())
        with self.assertRaises(AuthenticationFailed):
            update_account_profile(
                authenticated_session=self.credentials.session,
                values={"profile_version": self.version, "display_name": "No write"},
            )
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "Alex Morgan")

    def test_rotation_between_authentication_and_write_is_rechecked(self):
        rotate_account_session(self.credentials.refresh_token)
        with self.assertRaises(AuthenticationFailed):
            update_account_profile(
                authenticated_session=self.credentials.session,
                values={"profile_version": self.version, "display_name": "No write"},
            )


class AccountProfileConcurrencyTests(TransactionTestCase):
    def test_only_one_edit_with_the_same_observed_version_wins(self):
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock regression requires disposable PostgreSQL")
        user = get_user_model().objects.create_user(
            email="concurrent-profile@example.com", first_name="Original"
        )
        credentials = credentials_for(user)
        version = profile_version_for_user(user)
        barrier = Barrier(2)

        def edit(name):
            close_old_connections()
            try:
                session = CommunityChatAccountSession.objects.get(
                    pk=credentials.session.pk
                )
                barrier.wait(timeout=10)
                try:
                    update_account_profile(
                        authenticated_session=session,
                        values={"profile_version": version, "display_name": name},
                    )
                    return "saved"
                except ProfileVersionConflict:
                    return "conflict"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(
                workers.map(edit, ["First writer", "Second writer"], timeout=15)
            )
        self.assertCountEqual(results, ["saved", "conflict"])
        user.refresh_from_db()
        self.assertIn(user.full_name, {"First writer", "Second writer"})
