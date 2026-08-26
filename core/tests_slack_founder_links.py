from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import (
    SimpleTestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from core.models import (
    SlackFounderAccountLink,
    SlackFounderLinkRequest,
    User,
)
from core.slack_founder_links import (
    ConflictingSlackFounderLinkError,
    SlackFounderLinkError,
    SlackFounderLinkUserNotFoundError,
    UsedSlackFounderLinkError,
    assign_direct_slack_identity,
    complete_slack_founder_link,
    create_slack_founder_link_request,
    digest_link_token,
    start_slack_founder_link,
)
from core.slack_founder_link_retention import (
    run_scheduled_slack_founder_link_request_cleanup,
)
from core.slack_users import (
    SlackProfileUnavailableError,
    register_slack_side_user_for_founder_link,
    resolve_existing_user_from_profile,
)
from integrations.services.slack import (
    SlackService,
    SlackUserLookupUnavailableError,
    SlackUserNotFoundError,
)


class SlackFounderLinkSettingsTests(SimpleTestCase):
    def test_token_ttl_cannot_exceed_frontend_cookie_lifetime(self):
        environment = os.environ.copy()
        environment["ROO_FOUNDER_LINK_TTL_SECONDS"] = "1801"
        result = subprocess.run(
            [sys.executable, "-c", "import mlai.settings"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "ROO_FOUNDER_LINK_TTL_SECONDS must be between 300 and 1800 seconds",
            result.stdout + result.stderr,
        )


class SlackServiceStrictUserLookupTests(SimpleTestCase):
    @patch.object(
        SlackService,
        "get_client",
        side_effect=RuntimeError("token setup failed for UPRIVATE1"),
    )
    def test_client_initialization_failure_is_typed_and_sanitized(self, _get_client):
        with self.assertLogs(
            "integrations.services.slack",
            level="WARNING",
        ) as captured:
            with self.assertRaises(SlackUserLookupUnavailableError):
                SlackService.get_user_profile_strict("UPRIVATE1")

        output = "\n".join(captured.output)
        self.assertNotIn("UPRIVATE1", output)
        self.assertIn("reason_code=RuntimeError", output)

    @patch.object(SlackService, "get_client")
    def test_not_found_is_typed_and_logs_no_slack_id(self, get_client):
        get_client.return_value.users_info.return_value = {
            "ok": False,
            "error": "user_not_found",
        }

        with self.assertLogs(
            "integrations.services.slack",
            level="INFO",
        ) as captured:
            with self.assertRaises(SlackUserNotFoundError):
                SlackService.get_user_profile_strict("UMISSING1")

        self.assertNotIn("UMISSING1", "\n".join(captured.output))
        self.assertIn("reason_code=user_not_found", "\n".join(captured.output))

    @patch.object(SlackService, "get_client")
    def test_outage_is_typed_and_logs_no_slack_id(self, get_client):
        get_client.return_value.users_info.return_value = {
            "ok": False,
            "error": "team_access_not_granted",
        }

        with self.assertLogs(
            "integrations.services.slack",
            level="WARNING",
        ) as captured:
            with self.assertRaises(SlackUserLookupUnavailableError):
                SlackService.get_user_profile_strict("UPRIVATE1")

        self.assertNotIn("UPRIVATE1", "\n".join(captured.output))
        self.assertIn(
            "reason_code=team_access_not_granted",
            "\n".join(captured.output),
        )

    @patch.object(SlackService, "get_client")
    def test_malformed_profile_is_typed_and_logs_no_slack_id(self, get_client):
        get_client.return_value.users_info.return_value = {
            "ok": True,
            "user": {"id": "UPRIVATE1", "profile": None},
        }

        with self.assertLogs(
            "integrations.services.slack",
            level="WARNING",
        ) as captured:
            with self.assertRaises(SlackUserLookupUnavailableError):
                SlackService.get_user_profile_strict("UPRIVATE1")

        output = "\n".join(captured.output)
        self.assertNotIn("UPRIVATE1", output)
        self.assertIn("reason_code=malformed_success", output)

    @patch.object(SlackService, "get_client")
    def test_legacy_lookup_remains_nullable(self, get_client):
        get_client.return_value.users_info.return_value = {
            "ok": False,
            "error": "user_not_found",
        }

        self.assertIsNone(SlackService.get_user_profile("UMISSING1"))


class SlackFounderSyntheticIdentityMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0060_slackfounderlinkrequest_consumed_by_user")]
    migrate_to = [("core", "0061_clear_synthetic_web_slack_ids")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldUser = old_apps.get_model("core", "User")

        canonical = OldUser.objects.create(email="canonical-web@example.com")
        canonical.slack_id = f"web_{canonical.pk}"
        canonical.save(update_fields=["slack_id"])
        self.canonical_pk = canonical.pk

        noncanonical = OldUser.objects.create(
            email="noncanonical-web@example.com",
            slack_id="web_unrelated",
        )
        self.noncanonical_pk = noncanonical.pk
        real = OldUser.objects.create(
            email="real-slack@example.com",
            slack_id="UREAL12345",
        )
        self.real_pk = real.pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_only_canonical_synthetic_web_identity_is_cleared(self):
        MigratedUser = self.apps.get_model("core", "User")

        self.assertIsNone(
            MigratedUser.objects.get(pk=self.canonical_pk).slack_id
        )
        self.assertEqual(
            MigratedUser.objects.get(pk=self.noncanonical_pk).slack_id,
            "web_unrelated",
        )
        self.assertEqual(
            MigratedUser.objects.get(pk=self.real_pk).slack_id,
            "UREAL12345",
        )


class SlackFounderLinkLegacyCommandTests(TransactionTestCase):
    def setUp(self):
        self.slack_user = User.objects.create_user(
            email="slack-placeholder@example.com",
            slack_id="ULEGACY123",
        )
        self.founder_user = User.objects.create_user(
            email="founder@example.com",
        )
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

    @patch(
        "core.management.commands.reconcile_slack_users_by_email.SlackService"
    )
    def test_email_reconciliation_does_not_move_a_linked_slack_identity(
        self,
        slack_service_class,
    ):
        slack_service_class.return_value.get_user_profile.return_value = {
            "email": self.founder_user.email
        }
        output = StringIO()

        call_command(
            "reconcile_slack_users_by_email",
            "--commit",
            stdout=output,
        )

        self.slack_user.refresh_from_db()
        self.founder_user.refresh_from_db()
        self.assertEqual(self.slack_user.slack_id, "ULEGACY123")
        self.assertIsNone(self.founder_user.slack_id)
        self.assertIn("manual support required", output.getvalue())

    def test_cleanup_merge_refuses_accounts_with_an_explicit_link(self):
        from core.management.commands.cleanup_users import Command

        command = Command(stdout=StringIO(), stderr=StringIO())

        with self.assertRaisesMessage(
            CommandError,
            "explicit Roo-Founder Tools link",
        ):
            command.merge_users(self.slack_user, self.founder_user)

        self.assertTrue(User.objects.filter(pk=self.slack_user.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.founder_user.pk).exists())
        self.assertTrue(
            SlackFounderAccountLink.objects.filter(
                slack_user=self.slack_user,
                founder_user=self.founder_user,
            ).exists()
        )

    @patch(
        "core.management.commands.reconcile_slack_users_by_email.SlackService"
    )
    def test_reconciliation_invalidates_a_pending_link_before_identity_transfer(
        self,
        slack_service_class,
    ):
        SlackFounderAccountLink.objects.all().delete()
        request, _ = create_slack_founder_link_request(self.slack_user)
        slack_service_class.return_value.get_user_profile.return_value = {
            "email": self.founder_user.email
        }

        call_command(
            "reconcile_slack_users_by_email",
            "--commit",
            stdout=StringIO(),
        )

        request.refresh_from_db()
        self.slack_user.refresh_from_db()
        self.founder_user.refresh_from_db()
        self.assertIsNotNone(request.invalidated_at)
        self.assertIsNone(self.slack_user.slack_id)
        self.assertEqual(self.founder_user.slack_id, "ULEGACY123")

    @patch(
        "core.management.commands.reconcile_slack_users_by_email.SlackService"
    )
    def test_reconciliation_does_not_overwrite_identity_added_before_transfer(
        self,
        slack_service_class,
    ):
        SlackFounderAccountLink.objects.all().delete()
        slack_service_class.return_value.get_user_profile.return_value = {
            "email": self.founder_user.email
        }
        participation_checks = 0

        def add_identity_after_locked_rows_are_loaded(user):
            nonlocal participation_checks
            participation_checks += 1
            if participation_checks == 3:
                user.slack_id = "UCONCURRENT"
                user.save(update_fields=["slack_id"])
            return False

        output = StringIO()
        with patch(
            "core.management.commands.reconcile_slack_users_by_email."
            "user_participates_in_slack_founder_link",
            side_effect=add_identity_after_locked_rows_are_loaded,
        ):
            call_command(
                "reconcile_slack_users_by_email",
                "--commit",
                stdout=output,
            )

        self.slack_user.refresh_from_db()
        self.founder_user.refresh_from_db()
        self.assertEqual(self.slack_user.slack_id, "ULEGACY123")
        self.assertEqual(self.founder_user.slack_id, "UCONCURRENT")
        self.assertIn("target gained another Slack identity", output.getvalue())


class SlackIdentityLogPrivacyTests(TransactionTestCase):
    def test_profile_mismatch_log_omits_slack_identifiers(self):
        requested_slack_id = "UREQUEST12"
        returned_slack_id = "URETURN123"

        with self.assertLogs("core.slack_users", level="WARNING") as logs:
            result = resolve_existing_user_from_profile(
                slack_user_id=requested_slack_id,
                profile={
                    "slack_id": returned_slack_id,
                    "email": "private@example.com",
                },
            )

        self.assertIsNone(result)
        output = "\n".join(logs.output)
        self.assertNotIn(requested_slack_id, output)
        self.assertNotIn(returned_slack_id, output)
        self.assertNotIn("private@example.com", output)


@override_settings(
    ROO_API_KEY="roo-link-key",
    FOUNDER_TOOLS_URL="https://mlai.test",
    ROO_FOUNDER_LINK_TTL_SECONDS=1800,
    CSRF_TRUSTED_ORIGINS=["https://mlai.test"],
)
class SlackFounderLinkApiTests(APITestCase):
    def setUp(self):
        self.slack_user = User.objects.create_user(
            email="slack-account@example.com",
            slack_id="ULINK12345",
            first_name="Slack",
            last_name="Founder",
        )
        self.founder_user = User.objects.create_user(
            email="founder-tools@example.com",
        )
        self.start_url = reverse("slack_founder_link_start")
        self.status_url = reverse("slack_founder_link_status")
        self.preview_url = reverse("slack_founder_link_preview")
        self.complete_url = reverse("slack_founder_link_complete")

    def _start(self):
        return self.client.post(
            self.start_url,
            {"slack_user_id": self.slack_user.slack_id},
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

    def _token_from_response(self, response):
        return parse_qs(urlparse(response.data["link_url"]).query)["token"][0]

    def test_start_requires_strict_roo_key(self):
        response = self.client.post(
            self.start_url,
            {"slack_user_id": self.slack_user.slack_id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_start_returns_hashed_expiring_single_use_token(self):
        response = self._start()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Pragma"], "no-cache")
        self.assertEqual(response.data["status"], "link_required")
        self.assertTrue(
            response.data["link_url"].startswith(
                "https://mlai.test/founder-tools/link-roo?token="
            )
        )
        token = self._token_from_response(response)
        link_request = SlackFounderLinkRequest.objects.get()
        self.assertNotEqual(link_request.token_digest, token)
        self.assertEqual(link_request.token_digest, digest_link_token(token))
        self.assertGreater(link_request.expires_at, timezone.now())
        self.assertLessEqual(
            link_request.expires_at,
            timezone.now() + timedelta(minutes=31),
        )

    def test_new_start_invalidates_previous_unused_token(self):
        first = self._start()
        first_token = self._token_from_response(first)
        second = self._start()

        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        first_request = SlackFounderLinkRequest.objects.get(
            token_digest=digest_link_token(first_token)
        )
        self.assertIsNotNone(first_request.invalidated_at)

        self.client.force_authenticate(self.founder_user)
        preview = self.client.post(
            self.preview_url,
            {"token": first_token},
            format="json",
        )
        self.assertEqual(preview.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(preview.data["code"], "invalid_token")

    def test_failed_replacement_rolls_back_previous_request_invalidation(self):
        first = self._start()
        first_token = self._token_from_response(first)

        with (
            patch.object(
                SlackFounderLinkRequest.objects,
                "create",
                side_effect=RuntimeError("injected persistence failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            start_slack_founder_link(self.slack_user)

        first_request = SlackFounderLinkRequest.objects.get(
            token_digest=digest_link_token(first_token)
        )
        self.assertIsNone(first_request.invalidated_at)
        self.assertIsNone(first_request.consumed_at)

    @patch("core.slack_users._slack_profile")
    def test_start_registers_slack_side_without_matching_profile_email(
        self,
        slack_profile,
    ):
        slack_profile.return_value = {
            "slack_id": "UNEWLINK12",
            "email": self.founder_user.email,
            "real_name": "Separate Slack Member",
            "image_url": "https://example.com/avatar.png",
            "is_bot": False,
            "deleted": False,
        }

        response = self.client.post(
            self.start_url,
            {"slack_user_id": "UNEWLINK12"},
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        slack_user = User.objects.get(slack_id="UNEWLINK12")
        self.assertNotEqual(slack_user.pk, self.founder_user.pk)
        self.assertEqual(slack_user.email, "unewlink12@slack.placeholder.com")
        self.assertEqual(slack_user.full_name, "Separate Slack Member")
        self.assertEqual(
            SlackFounderLinkRequest.objects.get().slack_user,
            slack_user,
        )
        self.founder_user.refresh_from_db()
        self.assertIsNone(self.founder_user.slack_id)

    @patch(
        "core.slack_users._slack_profile",
        side_effect=SlackProfileUnavailableError,
    )
    def test_start_returns_retryable_error_when_slack_cannot_be_verified(
        self,
        _slack_profile,
    ):
        response = self.client.post(
            self.start_url,
            {"slack_user_id": "UMISSING1"},
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "slack_identity_unavailable")
        self.assertFalse(User.objects.filter(slack_id="UMISSING1").exists())
        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    @patch("core.slack_users._slack_profile", return_value=None)
    def test_start_returns_not_found_when_slack_confirms_user_is_missing(
        self,
        _slack_profile,
    ):
        response = self.client.post(
            self.start_url,
            {"slack_user_id": "UMISSING1"},
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "slack_user_not_found")
        self.assertFalse(User.objects.filter(slack_id="UMISSING1").exists())
        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    @patch("core.slack_users._slack_profile")
    def test_start_does_not_adopt_preexisting_placeholder_email(self, slack_profile):
        collision = User.objects.create_user(
            email="ucollision1@slack.placeholder.com",
            role="participant",
        )
        slack_profile.return_value = {
            "slack_id": "UCOLLISION1",
            "email": "untrusted-profile@example.com",
            "real_name": "Collision Member",
            "is_bot": False,
            "deleted": False,
        }

        response = self.client.post(
            self.start_url,
            {"slack_user_id": "UCOLLISION1"},
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        collision.refresh_from_db()
        self.assertIsNone(collision.slack_id)
        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    @patch("core.slack_users._slack_profile")
    def test_start_returns_not_found_for_ineligible_slack_identity(
        self,
        slack_profile,
    ):
        slack_profile.return_value = {
            "slack_id": "UMISSING1",
            "deleted": True,
            "is_bot": False,
        }
        response = self.client.post(
            self.start_url,
            {"slack_user_id": "UMISSING1"},
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "slack_user_not_found")

    @override_settings(
        ROO_FOUNDER_LINK_ISSUE_LIMIT=2,
        ROO_FOUNDER_LINK_ISSUE_WINDOW_SECONDS=600,
    )
    def test_start_rate_limit_preserves_latest_valid_request(self):
        first = self._start()
        second = self._start()
        second_token = self._token_from_response(second)

        limited = self._start()

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(limited.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(limited.data["code"], "link_rate_limited")
        self.assertGreaterEqual(limited.data["retry_after_seconds"], 1)
        self.assertEqual(
            limited["Retry-After"],
            str(limited.data["retry_after_seconds"]),
        )
        latest = SlackFounderLinkRequest.objects.get(
            token_digest=digest_link_token(second_token)
        )
        self.assertIsNone(latest.invalidated_at)
        self.assertEqual(SlackFounderLinkRequest.objects.count(), 2)

    def test_retention_cleanup_deletes_only_old_terminal_requests(self):
        now = timezone.now()
        stale_invalidated = SlackFounderLinkRequest.objects.create(
            slack_user=self.slack_user,
            token_digest=digest_link_token("A" * 43),
            expires_at=now - timedelta(days=8),
            invalidated_at=now - timedelta(days=8),
        )
        stale_expired = SlackFounderLinkRequest.objects.create(
            slack_user=self.slack_user,
            token_digest=digest_link_token("B" * 43),
            expires_at=now - timedelta(days=8),
        )
        recent_terminal = SlackFounderLinkRequest.objects.create(
            slack_user=self.slack_user,
            token_digest=digest_link_token("C" * 43),
            expires_at=now + timedelta(minutes=30),
            invalidated_at=now,
        )
        old_active = SlackFounderLinkRequest.objects.create(
            slack_user=self.slack_user,
            token_digest=digest_link_token("D" * 43),
            expires_at=now + timedelta(days=1),
        )
        SlackFounderLinkRequest.objects.filter(
            pk__in=[stale_invalidated.pk, stale_expired.pk, old_active.pk]
        ).update(created_at=now - timedelta(days=8))

        output = StringIO()
        call_command(
            "purge_slack_founder_link_requests",
            "--retention-days",
            "7",
            stdout=output,
        )

        self.assertEqual(
            set(SlackFounderLinkRequest.objects.values_list("pk", flat=True)),
            {recent_terminal.pk, old_active.pk},
        )
        self.assertIn(
            "Deleted 2 stale Slack-Founder link request(s).",
            output.getvalue(),
        )

    def test_retention_cleanup_rejects_invalid_window(self):
        with self.assertRaises(CommandError):
            call_command(
                "purge_slack_founder_link_requests",
                "--retention-days",
                "0",
            )

    @patch(
        "core.slack_founder_link_retention."
        "purge_stale_slack_founder_link_requests"
    )
    def test_scheduled_retention_runs_once_per_local_day(self, purge):
        cache.clear()
        self.addCleanup(cache.clear)
        purge.return_value = 3
        now = timezone.now()

        first = run_scheduled_slack_founder_link_request_cleanup(now=now)
        second = run_scheduled_slack_founder_link_request_cleanup(now=now)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["deleted"], 3)
        self.assertEqual(
            second,
            {
                "status": "skipped",
                "reason": "already_completed",
                "date": first["date"],
            },
        )
        purge.assert_called_once_with(now=now)

    @patch(
        "core.slack_founder_link_retention."
        "purge_stale_slack_founder_link_requests"
    )
    def test_failed_scheduled_retention_can_retry(self, purge):
        cache.clear()
        self.addCleanup(cache.clear)
        purge.side_effect = [RuntimeError("database busy"), 1]
        now = timezone.now()

        with self.assertRaises(RuntimeError):
            run_scheduled_slack_founder_link_request_cleanup(now=now)
        result = run_scheduled_slack_founder_link_request_cleanup(now=now)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(purge.call_count, 2)

    def test_start_rejects_an_inactive_slack_account(self):
        self.slack_user.is_active = False
        self.slack_user.save(update_fields=["is_active"])

        response = self._start()

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "slack_user_not_found")
        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    def test_complete_rejects_slack_account_deactivated_after_start(self):
        response = self._start()
        token = self._token_from_response(response)
        self.slack_user.is_active = False
        self.slack_user.save(update_fields=["is_active"])
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(complete.data["code"], "slack_user_not_found")
        self.assertFalse(SlackFounderAccountLink.objects.exists())

    def test_complete_rejects_founder_account_deactivated_after_start(self):
        response = self._start()
        token = self._token_from_response(response)
        self.founder_user.is_active = False
        self.founder_user.save(update_fields=["is_active"])
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")
        self.assertFalse(SlackFounderAccountLink.objects.exists())

    def test_start_rechecks_slack_identity_after_acquiring_the_user_lock(self):
        stale_slack_user = User.objects.get(pk=self.slack_user.pk)
        User.objects.filter(pk=self.slack_user.pk).update(slack_id="UREASSIGN1")

        with self.assertRaises(SlackFounderLinkUserNotFoundError):
            start_slack_founder_link(stale_slack_user)

        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    def test_preview_and_complete_require_authenticated_user(self):
        response = self._start()
        token = self._token_from_response(response)

        preview = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(preview.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(complete.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cookie_authenticated_preview_requires_exact_trusted_origin(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.cookies["access_token"] = str(
            AccessToken.for_user(self.founder_user)
        )

        missing_origin = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        untrusted_origin = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
            HTTP_ORIGIN="https://evil.example",
        )
        trusted_origin = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
            HTTP_ORIGIN="https://mlai.test",
        )

        self.assertEqual(missing_origin.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(untrusted_origin.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(trusted_origin.status_code, status.HTTP_200_OK)
        self.assertEqual(trusted_origin.data["status"], "ready")

    def test_status_requires_authenticated_user(self):
        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_status_reports_unconnected_founder_without_identity_details(self):
        self.client.force_authenticate(self.founder_user)

        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(
            response.data,
            {
                "status": "not_connected",
                "connection_type": None,
                "can_link_separate_account": True,
                "slack_display_name": None,
                "verified_at": None,
            },
        )
        self.assertNotIn("slack_id", response.data)
        self.assertNotIn("email", response.data)

    def test_status_treats_existing_same_account_identity_as_connected(self):
        self.client.force_authenticate(self.slack_user)

        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "connected")
        self.assertEqual(response.data["connection_type"], "direct")
        self.assertTrue(response.data["can_link_separate_account"])
        self.assertEqual(response.data["slack_display_name"], "Slack Founder")
        self.assertIsNone(response.data["verified_at"])
        self.assertNotIn("slack_id", response.data)

    def test_status_reports_explicit_verified_connection(self):
        link = SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )
        self.client.force_authenticate(self.founder_user)

        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "connected")
        self.assertEqual(response.data["connection_type"], "explicit")
        self.assertFalse(response.data["can_link_separate_account"])
        self.assertEqual(response.data["slack_display_name"], "Slack Founder")
        self.assertEqual(response.data["verified_at"], link.verified_at.isoformat())
        self.assertNotIn("slack_id", response.data)
        self.assertNotIn("email", response.data)

    def test_status_does_not_offer_relinking_to_explicit_slack_side(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )
        self.client.force_authenticate(self.slack_user)

        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["connection_type"], "direct")
        self.assertFalse(response.data["can_link_separate_account"])
        self.assertEqual(self._start().data, {"status": "already_linked"})

    def test_malformed_token_is_rejected_before_lookup(self):
        self.client.force_authenticate(self.founder_user)

        preview = self.client.post(
            self.preview_url,
            {"token": "not-a-valid-token"},
            format="json",
        )

        self.assertEqual(preview.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(preview.data["code"], "invalid_token")

    def test_authenticated_user_previews_and_confirms_link(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.founder_user)

        preview = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data["status"], "ready")
        self.assertEqual(preview.data["slack_display_name"], "Slack Founder")
        self.assertEqual(complete.status_code, status.HTTP_201_CREATED)
        self.assertEqual(complete.data["status"], "linked")
        link = SlackFounderAccountLink.objects.get()
        self.assertEqual(link.slack_user, self.slack_user)
        self.assertEqual(link.founder_user, self.founder_user)

    def test_failed_completion_rolls_back_link_and_token_consumption(self):
        response = self._start()
        token = self._token_from_response(response)

        with (
            patch.object(
                SlackFounderLinkRequest,
                "save",
                side_effect=RuntimeError("injected persistence failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            complete_slack_founder_link(
                token,
                founder_user=self.founder_user,
            )

        request = SlackFounderLinkRequest.objects.get(
            token_digest=digest_link_token(token)
        )
        self.assertIsNone(request.consumed_at)
        self.assertIsNone(request.consumed_by_user_id)
        self.assertFalse(SlackFounderAccountLink.objects.exists())

        completion = complete_slack_founder_link(
            token,
            founder_user=self.founder_user,
        )
        self.assertEqual(completion.status, "linked")

    def test_same_account_completion_is_a_noop_without_creating_a_trapping_link(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.slack_user)

        preview = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data["status"], "already_connected")
        self.assertEqual(complete.status_code, status.HTTP_200_OK)
        self.assertEqual(complete.data, {"status": "already_connected"})
        self.assertFalse(SlackFounderAccountLink.objects.exists())
        consumed_request = SlackFounderLinkRequest.objects.get()
        self.assertIsNotNone(consumed_request.consumed_at)
        self.assertEqual(consumed_request.consumed_by_user, self.slack_user)

        replay = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )
        self.assertEqual(replay.status_code, status.HTTP_409_CONFLICT)
        self.assertIs(
            replay.data["connection_matches_requesting_user"],
            True,
        )

        # A no-op same-account confirmation must not prevent a later request
        # for the user's actual separate Founder Tools account.
        follow_up = self._start()
        self.assertEqual(follow_up.status_code, status.HTTP_201_CREATED)
        self.assertEqual(follow_up.data["status"], "link_required")

    def test_linking_does_not_transfer_identity_or_points_records(self):
        from roo.models import CoworkingBooking, Ledger, PointsAccount

        slack_account = PointsAccount.objects.create(
            user=self.slack_user,
            balance=17,
            earned_balance=17,
            lifetime_earned=17,
        )
        founder_account = PointsAccount.objects.create(
            user=self.founder_user,
            balance=91,
            earned_balance=91,
            lifetime_earned=91,
        )
        ledger = Ledger.objects.create(
            user=self.slack_user,
            delta=-8,
            kind="SPEND",
            source="COWORKING",
            created_by_slack_id=self.slack_user.slack_id,
            idempotency_key="pre-link-ledger",
        )
        booking = CoworkingBooking.objects.create(
            user=self.slack_user,
            date=date.today() + timedelta(days=1),
            points_cost=8,
            ledger_entry=ledger,
        )
        original_identity = {
            "slack_email": self.slack_user.email,
            "slack_id": self.slack_user.slack_id,
            "founder_email": self.founder_user.email,
            "founder_slack_id": self.founder_user.slack_id,
        }

        response = self._start()
        self.client.force_authenticate(self.founder_user)
        complete = self.client.post(
            self.complete_url,
            {"token": self._token_from_response(response)},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_201_CREATED)
        self.slack_user.refresh_from_db()
        self.founder_user.refresh_from_db()
        slack_account.refresh_from_db()
        founder_account.refresh_from_db()
        ledger.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(
            {
                "slack_email": self.slack_user.email,
                "slack_id": self.slack_user.slack_id,
                "founder_email": self.founder_user.email,
                "founder_slack_id": self.founder_user.slack_id,
            },
            original_identity,
        )
        self.assertEqual(slack_account.balance, 17)
        self.assertEqual(founder_account.balance, 91)
        self.assertEqual(ledger.user, self.slack_user)
        self.assertEqual(booking.user, self.slack_user)

    def test_consumed_token_replay_is_rejected(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.founder_user)
        first = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )
        second = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(second.data["code"], "token_already_used")
        self.assertIs(
            second.data["connection_matches_requesting_user"],
            True,
        )

        preview = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        self.assertEqual(preview.status_code, status.HTTP_409_CONFLICT)
        self.assertIs(
            preview.data["connection_matches_requesting_user"],
            True,
        )

    def test_consumed_token_recovery_remains_truthful_after_original_expiry(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.founder_user)
        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )
        SlackFounderLinkRequest.objects.update(
            expires_at=timezone.now() - timedelta(days=1)
        )

        replay = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_201_CREATED)
        self.assertEqual(replay.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(replay.data["code"], "token_already_used")
        self.assertIs(
            replay.data["connection_matches_requesting_user"],
            True,
        )

    def test_consumed_token_does_not_confirm_a_different_founder_user(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.founder_user)
        self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )
        other_founder = User.objects.create_user(email="other@example.com")
        self.client.force_authenticate(other_founder)

        replay = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(replay.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(replay.data["code"], "token_already_used")
        self.assertIs(
            replay.data["connection_matches_requesting_user"],
            False,
        )

    def test_consumed_token_does_not_confirm_the_slack_user_after_other_founder_linked(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.founder_user)
        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )
        self.assertEqual(complete.status_code, status.HTTP_201_CREATED)

        self.client.force_authenticate(self.slack_user)
        replay = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(replay.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(replay.data["code"], "token_already_used")
        self.assertIs(
            replay.data["connection_matches_requesting_user"],
            False,
        )

    def test_expired_token_is_rejected(self):
        response = self._start()
        token = self._token_from_response(response)
        SlackFounderLinkRequest.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_410_GONE)
        self.assertEqual(complete.data["code"], "expired_token")

    def test_different_valid_request_for_same_pair_is_idempotent(self):
        first = self._start()
        self.client.force_authenticate(self.founder_user)
        self.client.post(
            self.complete_url,
            {"token": self._token_from_response(first)},
            format="json",
        )
        second_request, second_token = create_slack_founder_link_request(
            self.slack_user
        )

        complete = self.client.post(
            self.complete_url,
            {"token": second_token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_200_OK)
        self.assertEqual(complete.data["status"], "already_linked")
        second_request.refresh_from_db()
        self.assertIsNotNone(second_request.consumed_at)
        self.assertEqual(SlackFounderAccountLink.objects.count(), 1)

    def test_start_reports_existing_explicit_link_without_new_token(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        response = self._start()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"status": "already_linked"})
        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    def test_start_service_checks_existing_link_under_the_user_lock(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        result = start_slack_founder_link(self.slack_user)

        self.assertEqual(result.status, "already_linked")
        self.assertIsNone(result.request)
        self.assertIsNone(result.raw_token)
        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    def test_conflicting_explicit_slack_link_is_rejected(self):
        other_founder = User.objects.create_user(email="other-founder@example.com")
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=other_founder,
        )
        _, token = create_slack_founder_link_request(self.slack_user)
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")
        self.assertEqual(SlackFounderAccountLink.objects.count(), 1)

    def test_conflicting_founder_link_is_rejected(self):
        other_slack = User.objects.create_user(
            email="other-slack@example.com",
            slack_id="UOTHER1234",
        )
        SlackFounderAccountLink.objects.create(
            slack_user=other_slack,
            founder_user=self.founder_user,
        )
        _, token = create_slack_founder_link_request(self.slack_user)
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")

    def test_cross_role_explicit_link_is_rejected(self):
        other_slack = User.objects.create_user(
            email="cross-role-slack@example.com",
            slack_id="UCROSS1234",
        )
        SlackFounderAccountLink.objects.create(
            slack_user=other_slack,
            founder_user=self.slack_user,
        )
        _, token = create_slack_founder_link_request(self.slack_user)
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")

    def test_founder_with_different_direct_slack_identity_is_rejected(self):
        self.founder_user.slack_id = "UDIRECT123"
        self.founder_user.save(update_fields=["slack_id"])
        _, token = create_slack_founder_link_request(self.slack_user)
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")

    def test_database_enforces_unique_slack_and_founder_references(self):
        link = SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )
        other_slack = User.objects.create_user(
            email="constraint-slack@example.com",
            slack_id="UUNIQUE123",
        )
        other_founder = User.objects.create_user(
            email="constraint-founder@example.com",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            SlackFounderAccountLink.objects.create(
                slack_user=self.slack_user,
                founder_user=other_founder,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SlackFounderAccountLink.objects.create(
                slack_user=other_slack,
                founder_user=self.founder_user,
            )

        self.assertEqual(SlackFounderAccountLink.objects.get(), link)

    def test_database_rejects_self_links(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SlackFounderAccountLink.objects.create(
                slack_user=self.slack_user,
                founder_user=self.slack_user,
            )

        self.assertFalse(SlackFounderAccountLink.objects.exists())

    def test_legacy_link_slack_cannot_add_a_second_identity_to_linked_founder(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        response = self.client.post(
            reverse("link_slack"),
            {
                "slack_id": "USECOND123",
                "email": self.founder_user.email,
            },
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "link_conflict")
        self.founder_user.refresh_from_db()
        self.assertIsNone(self.founder_user.slack_id)

    def test_legacy_link_slack_cannot_reassign_explicit_slack_side(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        response = self.client.post(
            reverse("link_slack"),
            {
                "slack_id": "UNEWSIDE12",
                "email": self.slack_user.email,
            },
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "link_conflict")
        self.slack_user.refresh_from_db()
        self.assertEqual(self.slack_user.slack_id, "ULINK12345")

    def test_slack_registration_keeps_explicit_founder_as_a_separate_account(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        response = self.client.post(
            reverse("get_or_create_slack_user"),
            {
                "slack_id": "USECOND123",
                "email": self.founder_user.email,
                "first_name": "Second",
            },
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data["email"],
            "usecond123@slack.placeholder.com",
        )
        self.assertEqual(response.data["slack_id"], "USECOND123")
        self.founder_user.refresh_from_db()
        self.assertIsNone(self.founder_user.slack_id)

    def test_slack_registration_cannot_reassign_explicit_slack_side(self):
        from core.slack_users import ensure_slack_user

        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        result = ensure_slack_user(
            slack_id="UNEWSIDE12",
            email=self.slack_user.email,
            first_name="New",
        )

        self.assertNotEqual(result.user.pk, self.slack_user.pk)
        self.assertEqual(result.user.email, "unewside12@slack.placeholder.com")
        self.slack_user.refresh_from_db()
        self.assertEqual(self.slack_user.slack_id, "ULINK12345")

    def test_existing_explicit_slack_side_does_not_adopt_founder_email(self):
        from core.slack_users import ensure_slack_user

        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        result = ensure_slack_user(
            slack_id=self.slack_user.slack_id,
            email=self.founder_user.email,
            first_name="Updated",
        )

        self.assertEqual(result.user.pk, self.slack_user.pk)
        self.slack_user.refresh_from_db()
        self.founder_user.refresh_from_db()
        self.assertEqual(self.slack_user.email, "slack-account@example.com")
        self.assertEqual(self.founder_user.email, "founder-tools@example.com")

    def test_direct_identity_change_invalidates_unused_link_requests(self):
        request, token = create_slack_founder_link_request(self.slack_user)

        assign_direct_slack_identity(
            self.slack_user,
            "UNEWSIDE12",
            allow_reassignment=True,
        )

        request.refresh_from_db()
        self.assertIsNotNone(request.invalidated_at)
        self.client.force_authenticate(self.founder_user)
        response = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "invalid_token")

    def test_content_factory_actor_does_not_persist_synthetic_slack_identity(self):
        from integrations.api_views_content_factory_app import _ensure_actor_id

        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        actor_id = _ensure_actor_id(self.founder_user)

        self.assertEqual(actor_id, f"web_{self.founder_user.pk}")
        self.founder_user.refresh_from_db()
        self.assertIsNone(self.founder_user.slack_id)


@override_settings(
    COWORKING_DAY_COST_POINTS=8,
    COWORKING_DAY_DISCOUNT_COST_POINTS=4,
    DEFAULT_COWORKING_CAPACITY=20,
    INTERNAL_API_KEY="internal-key",
    ROO_API_KEY="roo-link-key",
)
class LinkedCoworkingEligibilityTests(APITestCase):
    def setUp(self):
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from organizations.models import Organization
        from roo.models import PointsAccount, PointsAdmin
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        self.slack_user = User.objects.create_user(
            email="roo-points@example.com",
            slack_id="UCOWLINK12",
        )
        self.founder_user = User.objects.create_user(
            email="monthly-updates@example.com",
        )
        self.admin_user = User.objects.create_user(
            email="points-admin@example.com",
            slack_id="ULINKADMIN",
        )
        PointsAdmin.objects.create(
            user=self.admin_user,
            slack_user_id=self.admin_user.slack_id,
            role="admin",
        )
        PointsAccount.objects.create(
            user=self.slack_user,
            balance=20,
            earned_balance=20,
            lifetime_earned=20,
        )
        PointsAccount.objects.create(
            user=self.founder_user,
            balance=99,
            earned_balance=99,
            lifetime_earned=99,
        )
        organization = Organization.objects.create(
            name="Linked Founder Pty Ltd",
            domain="linked-founder.example",
        )
        profile = VibeRaisingProfile.objects.create(
            user=self.founder_user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        VibeRaisingCompany.objects.create(
            profile=profile,
            organization=organization,
            name="Linked Founder Pty Ltd",
            registered=True,
            abn="89000000019",
            acn="000000019",
            abr_verified_at=timezone.now(),
        )
        UserStartupBinding.objects.create(
            user=self.founder_user,
            organization=organization,
            coworking_discount_eligible=True,
        )
        self.booking_date = date.today() + timedelta(days=1)
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=self.booking_date.replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

    def _add_slack_side_eligibility(self, *, suffix: str):
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from organizations.models import Organization
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        organization = Organization.objects.create(
            name=f"Slack Account Founder {suffix}",
            domain=f"slack-account-founder-{suffix}.example",
        )
        profile = VibeRaisingProfile.objects.create(
            user=self.slack_user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        VibeRaisingCompany.objects.create(
            profile=profile,
            organization=organization,
            name=f"Slack Account Founder {suffix}",
            registered=True,
            abn="89000000020",
            acn="000000020",
            abr_verified_at=timezone.now(),
        )
        UserStartupBinding.objects.create(
            user=self.slack_user,
            organization=organization,
            coworking_discount_eligible=True,
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=self.booking_date.replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )

    def test_linked_founder_eligibility_discounts_without_moving_points(self):
        from roo.models import Ledger, PointsAccount
        from roo.services import CoworkingService

        booking, created = CoworkingService.book(
            user=self.slack_user,
            booking_date=self.booking_date,
            created_by_slack_id=self.slack_user.slack_id,
        )

        self.assertTrue(created)
        self.assertEqual(booking.user, self.slack_user)
        self.assertEqual(booking.points_cost, 4)
        self.assertEqual(booking.ledger_entry.user, self.slack_user)
        self.assertEqual(
            Ledger.objects.get(pk=booking.ledger_entry_id).created_by_slack_id,
            self.slack_user.slack_id,
        )
        self.assertEqual(
            PointsAccount.objects.get(user=self.slack_user).balance,
            16,
        )
        self.assertEqual(
            PointsAccount.objects.get(user=self.founder_user).balance,
            99,
        )

    def test_inactive_linked_founder_does_not_confer_discount_eligibility(self):
        from roo.services import CoworkingService

        self._add_slack_side_eligibility(suffix="inactive-link")
        self.founder_user.is_active = False
        self.founder_user.save(update_fields=["is_active"])

        self.assertEqual(
            CoworkingService.get_coworking_cost(
                user=self.slack_user,
                booking_date=self.booking_date,
            ),
            8,
        )

    def test_availability_and_booking_api_use_linked_eligibility(self):
        availability = self.client.get(
            reverse("coworking-availability"),
            {
                "days": 1,
                "date": self.booking_date.isoformat(),
                "slack_user_id": self.slack_user.slack_id,
            },
            HTTP_X_API_KEY="internal-key",
        )
        booking = self.client.post(
            reverse("coworking-book"),
            {
                "slack_user_id": self.slack_user.slack_id,
                "date": self.booking_date.isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="internal-key",
        )

        self.assertEqual(availability.status_code, status.HTTP_200_OK)
        self.assertEqual(availability.data[0]["cost_points"], 4)
        self.assertEqual(booking.status_code, status.HTTP_201_CREATED)
        self.assertEqual(booking.data["points_cost"], 4)
        self.assertTrue(booking.data["monthly_update_discount_applied"])
        self.assertEqual(
            booking.data["founder_tools_connection_type"],
            "explicit",
        )
        self.assertTrue(booking.data["founder_tools_account_linked"])
        self.assertTrue(booking.data["founder_tools_explicitly_linked"])

    def test_explicit_link_replaces_slack_side_eligibility_fallback(self):
        from roo.services import CoworkingService
        from startup_updates.models import UserStartupBinding

        # Remove the linked founder's eligibility so only the Slack-side
        # account could qualify if the legacy fallback were still considered.
        UserStartupBinding.objects.filter(user=self.founder_user).update(
            coworking_discount_eligible=False
        )
        self._add_slack_side_eligibility(suffix="explicit-link")

        self.assertEqual(
            CoworkingService.get_coworking_cost(
                user=self.slack_user,
                booking_date=self.booking_date,
            ),
            8,
        )

    def test_existing_booking_is_not_repriced_after_linking(self):
        from roo.models import CoworkingBooking, Ledger, PointsAccount

        SlackFounderAccountLink.objects.all().delete()
        ledger = Ledger.objects.create(
            user=self.slack_user,
            delta=-8,
            kind="SPEND",
            source="COWORKING",
            created_by_slack_id=self.slack_user.slack_id,
            idempotency_key=f"legacy-booking-{self.slack_user.pk}",
        )
        existing = CoworkingBooking.objects.create(
            user=self.slack_user,
            date=self.booking_date,
            points_cost=8,
            ledger_entry=ledger,
        )
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )
        balance_before = PointsAccount.objects.get(user=self.slack_user).balance

        response = self.client.post(
            reverse("coworking-book"),
            {
                "slack_user_id": self.slack_user.slack_id,
                "date": self.booking_date.isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="internal-key",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_booked"])
        self.assertEqual(response.data["points_cost"], 8)
        self.assertEqual(response.data["founder_tools_connection_type"], "explicit")
        self.assertTrue(response.data["founder_tools_explicitly_linked"])
        existing.refresh_from_db()
        self.assertEqual(existing.points_cost, 8)
        self.assertEqual(
            PointsAccount.objects.get(user=self.slack_user).balance,
            balance_before,
        )

    def test_admin_batch_booking_uses_targets_linked_eligibility(self):
        from roo.models import PointsAccount

        response = self.client.post(
            reverse("coworking-book-many"),
            {
                "admin_slack_user_id": self.admin_user.slack_id,
                "target_slack_user_ids": [self.slack_user.slack_id],
                "date": self.booking_date.isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        result = response.data["results"][0]
        self.assertEqual(result["points_cost"], 4)
        self.assertTrue(result["monthly_update_discount_applied"])
        self.assertEqual(result["founder_tools_connection_type"], "explicit")
        self.assertTrue(result["founder_tools_explicitly_linked"])
        self.assertEqual(
            PointsAccount.objects.get(user=self.slack_user).balance,
            16,
        )
        self.assertEqual(
            PointsAccount.objects.get(user=self.founder_user).balance,
            99,
        )

    def test_direct_same_account_response_is_not_misreported_as_explicit_link(self):
        SlackFounderAccountLink.objects.all().delete()

        response = self.client.post(
            reverse("coworking-book"),
            {
                "slack_user_id": self.slack_user.slack_id,
                "date": self.booking_date.isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="internal-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["founder_tools_connection_type"], "direct")
        self.assertTrue(response.data["founder_tools_account_linked"])
        self.assertFalse(response.data["founder_tools_explicitly_linked"])

    def test_linked_update_cannot_discount_a_second_slack_identity(self):
        from core.slack_users import ensure_slack_user
        from roo.services import CoworkingService

        second_registration = ensure_slack_user(
            slack_id="USECOND123",
            email=self.founder_user.email,
            first_name="Second",
        )

        self.assertNotEqual(second_registration.user, self.founder_user)
        self.assertTrue(
            second_registration.user.email.endswith("@slack.placeholder.com")
        )
        self.assertEqual(
            CoworkingService.get_coworking_cost(
                user=second_registration.user,
                booking_date=self.booking_date,
            ),
            8,
        )


@override_settings(ROO_FOUNDER_LINK_TTL_SECONDS=1800)
class SlackFounderLinkConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_link_start_registration_creates_one_slack_side_user(self):
        slack_user_id = "URACENEW12"
        profile = {
            "slack_id": slack_user_id,
            "email": "founder-email-must-not-match@example.com",
            "real_name": "Concurrent Member",
            "is_bot": False,
            "deleted": False,
        }
        barrier = Barrier(2)

        def register():
            close_old_connections()
            barrier.wait()
            try:
                user = register_slack_side_user_for_founder_link(slack_user_id)
                return user.pk if user is not None else None
            finally:
                connection.close()

        with (
            patch("core.slack_users._slack_profile", return_value=profile),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            outcomes = list(executor.map(lambda _item: register(), range(2)))

        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(User.objects.filter(slack_id=slack_user_id).count(), 1)
        self.assertEqual(
            User.objects.get(slack_id=slack_user_id).email,
            "uracenew12@slack.placeholder.com",
        )

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_completion_creates_one_link_and_rejects_replay(self):
        slack_user = User.objects.create_user(
            email="concurrent-slack@example.com",
            slack_id="UCONCUR123",
        )
        founder_user = User.objects.create_user(
            email="concurrent-founder@example.com",
        )
        _, token = create_slack_founder_link_request(slack_user)
        barrier = Barrier(2)

        def complete():
            close_old_connections()
            barrier.wait()
            try:
                completion = complete_slack_founder_link(
                    token,
                    founder_user=founder_user,
                )
                return "created" if completion.created else "existing"
            except UsedSlackFounderLinkError:
                return "used"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: complete(), range(2)))

        self.assertCountEqual(outcomes, ["created", "used"])
        self.assertEqual(SlackFounderAccountLink.objects.count(), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_swapped_role_completions_lock_users_in_the_same_order(self):
        first_user = User.objects.create_user(
            email="lock-order-first@example.com",
            slack_id="ULOCKFIRST",
        )
        second_user = User.objects.create_user(
            email="lock-order-second@example.com",
            slack_id="ULOCKSECOND",
        )
        _, first_token = create_slack_founder_link_request(first_user)
        _, second_token = create_slack_founder_link_request(second_user)
        barrier = Barrier(2)

        def complete(token, founder_user):
            close_old_connections()
            barrier.wait()
            try:
                complete_slack_founder_link(
                    token,
                    founder_user=founder_user,
                )
                return "completed"
            except ConflictingSlackFounderLinkError:
                return "conflict"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(complete, first_token, second_user),
                executor.submit(complete, second_token, first_user),
            ]
            outcomes = [future.result(timeout=5) for future in futures]

        self.assertEqual(outcomes, ["conflict", "conflict"])
        self.assertFalse(SlackFounderAccountLink.objects.exists())

    @skipUnlessDBFeature("has_select_for_update")
    def test_start_and_completion_share_a_deadlock_free_lock_order(self):
        slack_user = User.objects.create_user(
            email="start-race-slack@example.com",
            slack_id="USTARTRACE",
        )
        founder_user = User.objects.create_user(
            email="start-race-founder@example.com",
        )
        _, token = create_slack_founder_link_request(slack_user)
        barrier = Barrier(2)

        def start():
            close_old_connections()
            barrier.wait()
            try:
                return f"start:{start_slack_founder_link(slack_user).status}"
            finally:
                connection.close()

        def complete():
            close_old_connections()
            barrier.wait()
            try:
                result = complete_slack_founder_link(
                    token,
                    founder_user=founder_user,
                )
                return f"complete:{result.status}"
            except SlackFounderLinkError as exc:
                return f"complete:{exc.code}"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start), executor.submit(complete)]
            outcomes = [future.result(timeout=5) for future in futures]

        self.assertIn(
            outcomes,
            [
                ["start:already_linked", "complete:linked"],
                ["start:link_required", "complete:invalid_token"],
            ],
        )

    @skipUnlessDBFeature("has_select_for_update")
    def test_completion_and_direct_identity_assignment_cannot_both_win(self):
        slack_user = User.objects.create_user(
            email="identity-race-slack@example.com",
            slack_id="UIDRACESLK",
        )
        founder_user = User.objects.create_user(
            email="identity-race-founder@example.com",
        )
        _, token = create_slack_founder_link_request(slack_user)
        barrier = Barrier(2)

        def complete():
            close_old_connections()
            barrier.wait()
            try:
                complete_slack_founder_link(token, founder_user=founder_user)
                return "linked"
            except ConflictingSlackFounderLinkError:
                return "link_conflict"
            finally:
                connection.close()

        def assign():
            close_old_connections()
            barrier.wait()
            try:
                assign_direct_slack_identity(founder_user, "UIDRACEWEB")
                return "assigned"
            except ConflictingSlackFounderLinkError:
                return "assignment_conflict"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(complete), executor.submit(assign)]
            outcomes = [future.result(timeout=5) for future in futures]

        founder_user.refresh_from_db()
        link_exists = SlackFounderAccountLink.objects.filter(
            slack_user=slack_user,
            founder_user=founder_user,
        ).exists()
        self.assertIn(
            outcomes,
            [
                ["linked", "assignment_conflict"],
                ["link_conflict", "assigned"],
            ],
        )
        self.assertEqual(link_exists, founder_user.slack_id is None)
