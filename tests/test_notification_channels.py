import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import (
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    OrganizationContentConfig,
    ResearchAutomation,
    ResearchAutomationStatus,
)
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.services.notification_channels import (
    ChannelActionError,
    build_email_verification_url,
    ensure_research_automation_for_org,
    initiate_email_channel,
    initiate_whatsapp_channel,
    link_slack_channel,
    normalize_e164,
    send_whatsapp_otp,
    verify_whatsapp_otp,
)
from organizations.models import Organization


User = get_user_model()


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


WHATSAPP_SETTINGS = dict(
    TWILIO_ACCOUNT_SID="AC-test",
    TWILIO_AUTH_TOKEN="twilio-auth-token",
    TWILIO_WHATSAPP_FROM="+61480000000",
    TWILIO_WHATSAPP_OTP_CONTENT_SID="HX-otp",
)


class WhatsAppOtpTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="OTP Co", domain="otp.example.com")
        self.user = User.objects.create_user(email="founder@example.com")

    def _channel(self):
        return initiate_whatsapp_channel(organization=self.org, user=self.user, phone="+61 400 000 000")

    def test_normalize_e164(self):
        self.assertEqual(normalize_e164("+61 400-000-000"), "+61400000000")
        self.assertEqual(normalize_e164("0061400000000"), "+61400000000")
        with self.assertRaises(ChannelActionError):
            normalize_e164("0400000000")
        with self.assertRaises(ChannelActionError):
            normalize_e164("+12")

    @override_settings(**WHATSAPP_SETTINGS)
    @patch("integrations.services.notification_channels._generate_otp", return_value="123456")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_send_otp_stores_hash_and_sends_auth_template(self, mock_post, _mock_otp):
        mock_post.return_value = _Response(201, {"sid": "SM-1"})
        channel = self._channel()
        send_whatsapp_otp(channel)

        channel.refresh_from_db()
        self.assertTrue(channel.verification_code_hash)
        self.assertNotIn("123456", channel.verification_code_hash)
        self.assertIsNotNone(channel.verification_expires_at)
        self.assertEqual(channel.verification_send_count, 1)

        url = mock_post.call_args.args[0]
        self.assertIn("api.twilio.com", url)
        self.assertIn("AC-test", url)
        self.assertEqual(mock_post.call_args.kwargs["auth"], ("AC-test", "twilio-auth-token"))
        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(payload["From"], "whatsapp:+61480000000")
        self.assertEqual(payload["To"], "whatsapp:+61400000000")
        self.assertEqual(payload["ContentSid"], "HX-otp")
        self.assertEqual(json.loads(payload["ContentVariables"]), {"1": "123456"})

    @override_settings(**WHATSAPP_SETTINGS)
    @patch("integrations.services.notification_channels._generate_otp", return_value="123456")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_verify_happy_path_activates_channel(self, mock_post, _mock_otp):
        mock_post.return_value = _Response(201, {"sid": "SM-1"})
        channel = self._channel()
        send_whatsapp_otp(channel)

        verify_whatsapp_otp(channel, "123456")

        channel.refresh_from_db()
        self.assertEqual(channel.consent_state, NotificationConsentState.ACTIVE)
        self.assertIsNotNone(channel.verified_at)
        self.assertEqual(channel.verification_code_hash, "")

    @override_settings(**WHATSAPP_SETTINGS)
    @patch("integrations.services.notification_channels._generate_otp", return_value="123456")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_wrong_code_attempts_then_lockout(self, mock_post, _mock_otp):
        mock_post.return_value = _Response(201, {"sid": "SM-1"})
        channel = self._channel()
        send_whatsapp_otp(channel)

        for attempt in range(1, 5):
            with self.assertRaises(ChannelActionError) as ctx:
                verify_whatsapp_otp(channel, "000000")
            self.assertEqual(ctx.exception.code, "invalid_code")
            self.assertEqual(channel.verification_attempts, attempt)

        with self.assertRaises(ChannelActionError) as ctx:
            verify_whatsapp_otp(channel, "000000")
        self.assertEqual(ctx.exception.code, "too_many_attempts")
        channel.refresh_from_db()
        self.assertEqual(channel.verification_code_hash, "")

        with self.assertRaises(ChannelActionError) as ctx:
            verify_whatsapp_otp(channel, "123456")
        self.assertEqual(ctx.exception.code, "no_pending_code")

    @override_settings(**WHATSAPP_SETTINGS)
    @patch("integrations.services.notification_channels._generate_otp", return_value="123456")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_expired_code_rejected(self, mock_post, _mock_otp):
        mock_post.return_value = _Response(201, {"sid": "SM-1"})
        channel = self._channel()
        send_whatsapp_otp(channel)
        NotificationChannel.objects.filter(pk=channel.pk).update(
            verification_expires_at=timezone.now() - timedelta(minutes=1)
        )
        channel.refresh_from_db()

        with self.assertRaises(ChannelActionError) as ctx:
            verify_whatsapp_otp(channel, "123456")
        self.assertEqual(ctx.exception.code, "expired")

    @override_settings(**WHATSAPP_SETTINGS)
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_resend_cooldown_and_invalidates_previous_code(self, mock_post):
        mock_post.return_value = _Response(201, {"sid": "SM-1"})
        channel = self._channel()
        with patch(
            "integrations.services.notification_channels._generate_otp", return_value="111111"
        ):
            send_whatsapp_otp(channel)

        with self.assertRaises(ChannelActionError) as ctx:
            send_whatsapp_otp(channel)
        self.assertEqual(ctx.exception.code, "resend_cooldown")
        self.assertEqual(ctx.exception.http_status, 429)

        NotificationChannel.objects.filter(pk=channel.pk).update(
            verification_last_sent_at=timezone.now() - timedelta(minutes=2)
        )
        channel.refresh_from_db()
        with patch(
            "integrations.services.notification_channels._generate_otp", return_value="222222"
        ):
            send_whatsapp_otp(channel)

        with self.assertRaises(ChannelActionError) as ctx:
            verify_whatsapp_otp(channel, "111111")
        self.assertEqual(ctx.exception.code, "invalid_code")
        verify_whatsapp_otp(channel, "222222")
        channel.refresh_from_db()
        self.assertEqual(channel.consent_state, NotificationConsentState.ACTIVE)

    @override_settings(**WHATSAPP_SETTINGS)
    @patch("integrations.services.notification_channels._generate_otp", return_value="123456")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_send_cap_per_rolling_day(self, mock_post, _mock_otp):
        mock_post.return_value = _Response(201, {"sid": "SM-1"})
        channel = self._channel()
        channel.verification_send_count = 8
        channel.verification_last_sent_at = timezone.now() - timedelta(minutes=5)
        channel.save()

        with self.assertRaises(ChannelActionError) as ctx:
            send_whatsapp_otp(channel)
        self.assertEqual(ctx.exception.code, "send_limit_reached")

        # A last send more than 24h ago resets the rolling window.
        NotificationChannel.objects.filter(pk=channel.pk).update(
            verification_last_sent_at=timezone.now() - timedelta(hours=25)
        )
        channel.refresh_from_db()
        send_whatsapp_otp(channel)
        channel.refresh_from_db()
        self.assertEqual(channel.verification_send_count, 1)

    def test_send_otp_without_template_returns_503(self):
        channel = self._channel()
        with self.assertRaises(ChannelActionError) as ctx:
            send_whatsapp_otp(channel)
        self.assertEqual(ctx.exception.code, "whatsapp_not_configured")
        self.assertEqual(ctx.exception.http_status, 503)


@override_settings(
    DEFAULT_BACKEND_URL="https://api.test",
    RESEND_API_KEY="resend-key",
    CUSTOMERIO_API_KEY="",
    FOUNDER_TOOLS_URL="https://app.test",
)
class EmailVerificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name="Email Co", domain="email.example.com")
        self.user = User.objects.create_user(email="founder@example.com")

    @patch("integrations.services.notification_adapters.http_client.post")
    def test_initiate_sends_signed_link_via_resend(self, mock_post):
        mock_post.return_value = _Response(200, {"id": "email-1"})
        channel = initiate_email_channel(organization=self.org, user=self.user, route_id="Founder@Example.com")
        self.assertEqual(channel.route_id, "founder@example.com")
        self.assertEqual(channel.consent_state, NotificationConsentState.PENDING)

        from integrations.services.notification_channels import send_email_verification

        send_email_verification(channel)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["to"], ["founder@example.com"])
        self.assertIn("/api/content-factory/notification-channels/verify-email", payload["text"])
        self.assertIn("token=", payload["text"])

    def test_verify_link_activates_and_redirects(self):
        channel = initiate_email_channel(organization=self.org, user=self.user)
        url = build_email_verification_url(channel)
        token = parse_qs(urlparse(url).query)["token"][0]

        response = self.client.get(
            reverse("content_factory_notification_channel_verify"), {"token": token}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(
            response["Location"],
            "https://app.test/founder-tools/marketing/settings?emailChannel=verified",
        )
        channel.refresh_from_db()
        self.assertEqual(channel.consent_state, NotificationConsentState.ACTIVE)
        self.assertIsNotNone(channel.verified_at)

    @override_settings(NOTIFICATION_CHANNEL_VERIFY_MAX_AGE_SECONDS=-1)
    def test_expired_link_redirects_expired(self):
        channel = initiate_email_channel(organization=self.org, user=self.user)
        token = parse_qs(urlparse(build_email_verification_url(channel)).query)["token"][0]

        response = self.client.get(
            reverse("content_factory_notification_channel_verify"), {"token": token}
        )

        self.assertTrue(response["Location"].endswith("emailChannel=expired"))
        channel.refresh_from_db()
        self.assertEqual(channel.consent_state, NotificationConsentState.PENDING)

    def test_route_mismatch_or_garbage_token_redirects_invalid(self):
        channel = initiate_email_channel(organization=self.org, user=self.user)
        token = parse_qs(urlparse(build_email_verification_url(channel)).query)["token"][0]
        channel.route_id = "changed@example.com"
        channel.save(update_fields=["route_id"])

        response = self.client.get(
            reverse("content_factory_notification_channel_verify"), {"token": token}
        )
        self.assertTrue(response["Location"].endswith("emailChannel=invalid"))

        response = self.client.get(
            reverse("content_factory_notification_channel_verify"), {"token": "garbage"}
        )
        self.assertTrue(response["Location"].endswith("emailChannel=invalid"))


class SlackLinkTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Slack Co", domain="slack.example.com")
        self.user = User.objects.create_user(email="founder@example.com")
        self.config, _ = OrganizationContentConfig.objects.get_or_create(organization=self.org)

    @patch("integrations.services.notification_channels.SlackService.send_dm", return_value=(True, "1.0"))
    @patch("integrations.services.notification_channels.SlackService.lookup_user_by_email")
    def test_link_via_user_slack_id(self, mock_lookup, mock_dm):
        self.user.slack_id = "U111"
        self.user.save(update_fields=["slack_id"])

        channel = link_slack_channel(organization=self.org, user=self.user, config=self.config)

        self.assertEqual(channel.route_id, "U111")
        self.assertEqual(channel.consent_state, NotificationConsentState.ACTIVE)
        mock_lookup.assert_not_called()
        mock_dm.assert_called_once()
        self.config.refresh_from_db()
        self.assertEqual(self.config.connected_slack_user_id, "U111")

    @patch("integrations.services.notification_channels.SlackService.send_dm", return_value=(True, "1.0"))
    @patch("integrations.services.notification_channels.SlackService.lookup_user_by_email")
    def test_link_via_config_connected_slack_user(self, mock_lookup, mock_dm):
        self.config.connected_slack_user_id = "U222"
        self.config.save(update_fields=["connected_slack_user_id"])

        channel = link_slack_channel(organization=self.org, user=self.user, config=self.config)

        self.assertEqual(channel.route_id, "U222")
        mock_lookup.assert_not_called()

    @patch("integrations.services.notification_channels.SlackService.send_dm", return_value=(True, "1.0"))
    @patch(
        "integrations.services.notification_channels.SlackService.lookup_user_by_email",
        return_value={"slack_id": "U333", "real_name": "Founder"},
    )
    def test_link_via_email_lookup_persists_slack_id(self, mock_lookup, mock_dm):
        channel = link_slack_channel(organization=self.org, user=self.user, config=self.config)

        self.assertEqual(channel.route_id, "U333")
        self.assertEqual(channel.display_name, "Founder")
        self.user.refresh_from_db()
        self.assertEqual(self.user.slack_id, "U333")

    @patch("integrations.services.notification_channels.SlackService.send_dm")
    @patch(
        "integrations.services.notification_channels.SlackService.lookup_user_by_email",
        return_value=None,
    )
    def test_unresolvable_slack_raises(self, mock_lookup, mock_dm):
        with self.assertRaises(ChannelActionError) as ctx:
            link_slack_channel(organization=self.org, user=self.user, config=self.config)
        self.assertEqual(ctx.exception.code, "slack_user_not_found")
        mock_dm.assert_not_called()


class EnsureAutomationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Ensure Co", domain="ensure.example.com")
        self.user = User.objects.create_user(email="founder@example.com")
        self.config, _ = OrganizationContentConfig.objects.get_or_create(organization=self.org)

    @patch("integrations.services.notification_channels.SlackService.send_dm", return_value=(True, "1.0"))
    def test_creates_slack_channel_and_automation_when_none_exist(self, _mock_dm):
        self.user.slack_id = "U900"
        self.user.save(update_fields=["slack_id"])

        automation, channels = ensure_research_automation_for_org(
            organization=self.org,
            user=self.user,
            timezone_name="Australia/Sydney",
            enabled=True,
            config=self.config,
        )

        self.assertIsNotNone(automation)
        self.assertEqual(automation.status, ResearchAutomationStatus.ACTIVE)
        self.assertEqual(automation.timezone, "Australia/Sydney")
        self.assertEqual(automation.local_send_times, ["08:00"])
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].channel_type, NotificationChannelType.SLACK)
        self.assertEqual(automation.notification_channel_id, channels[0].id)

    @patch(
        "integrations.services.notification_channels.SlackService.lookup_user_by_email",
        return_value=None,
    )
    def test_returns_none_when_no_channel_possible(self, _mock_lookup):
        automation, channels = ensure_research_automation_for_org(
            organization=self.org,
            user=self.user,
            enabled=True,
            config=self.config,
        )
        self.assertIsNone(automation)
        self.assertEqual(channels, [])

    def test_repoints_primary_when_inactive(self):
        slack = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.SLACK,
            route_id="U1",
            consent_state=NotificationConsentState.OPTED_OUT,
        )
        email = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.EMAIL,
            route_id="founder@example.com",
            consent_state=NotificationConsentState.ACTIVE,
        )
        automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=slack,
            status=ResearchAutomationStatus.PAUSED,
        )

        result, channels = ensure_research_automation_for_org(
            organization=self.org, user=self.user, enabled=True, config=self.config
        )

        self.assertEqual(result.id, automation.id)
        self.assertEqual(result.status, ResearchAutomationStatus.ACTIVE)
        self.assertEqual(result.notification_channel_id, email.id)
        self.assertEqual([channel.id for channel in channels], [email.id])


@override_settings(
    DEFAULT_BACKEND_URL="https://api.test",
    RESEND_API_KEY="",
    CUSTOMERIO_API_KEY="cio-key",
    CUSTOMERIO_FROM_EMAIL="Roo <roo@mlai.au>",
)
class CustomerioEmailVerificationTests(TestCase):
    """Customer.io is the preferred transport when its key is configured."""

    def setUp(self):
        self.org = Organization.objects.create(name="CIO Co", domain="cio.example.com")
        self.user = User.objects.create_user(email="founder@example.com")

    @patch("integrations.services.notification_adapters._customerio_client")
    def test_verification_email_sends_via_customerio(self, mock_cio):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.send_email.return_value = {"delivery_id": "dl-1"}
        mock_cio.return_value = client
        channel = initiate_email_channel(organization=self.org, user=self.user)

        from integrations.services.notification_channels import send_email_verification

        send_email_verification(channel)

        request_body = client.send_email.call_args.args[0]
        self.assertEqual(request_body["to"], "founder@example.com")
        self.assertEqual(request_body["identifiers"], {"id": str(self.user.id)})
        self.assertEqual(request_body["from"], "Roo <roo@mlai.au>")
        self.assertIn("/api/content-factory/notification-channels/verify-email", request_body["body_plain"])
        self.assertIn("token=", request_body["body_plain"])
        self.assertIn("Confirm this email", request_body["body"])

    @patch("integrations.services.notification_adapters._customerio_client")
    def test_customerio_failure_maps_to_send_failed(self, mock_cio):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.send_email.side_effect = RuntimeError("workspace rejected the send")
        mock_cio.return_value = client
        channel = initiate_email_channel(organization=self.org, user=self.user)

        from integrations.services.notification_channels import send_email_verification

        with self.assertRaises(ChannelActionError) as ctx:
            send_email_verification(channel)
        self.assertEqual(ctx.exception.code, "send_failed")

    @override_settings(CUSTOMERIO_API_KEY="", RESEND_API_KEY="")
    def test_no_provider_configured_returns_503(self):
        channel = initiate_email_channel(organization=self.org, user=self.user)

        from integrations.services.notification_channels import send_email_verification

        with self.assertRaises(ChannelActionError) as ctx:
            send_email_verification(channel)
        self.assertEqual(ctx.exception.code, "email_not_configured")
        self.assertEqual(ctx.exception.http_status, 503)


@override_settings(RESEND_API_KEY="resend-key", CUSTOMERIO_API_KEY="", DEFAULT_BACKEND_URL="https://api.test")
class NotificationChannelEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com",
            password="password",
            first_name="Founder",
            last_name="One",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            name="Acme",
            domain="acme.com",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        self.org = Organization.objects.create(name="Acme", domain="acme.com")
        self.client.force_authenticate(user=self.user)

    def test_list_channels_empty(self):
        response = self.client.get(reverse("vibe-marketing-notification-channels"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["channels"], [])
        self.assertIsNone(response.data["automation"])

    @patch("integrations.services.notification_adapters.http_client.post")
    def test_create_email_channel_sends_verification(self, mock_post):
        mock_post.return_value = _Response(200, {"id": "email-1"})
        response = self.client.post(
            reverse("vibe-marketing-notification-channels"),
            {"channelType": "email"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "verification_sent")
        channel = response.data["channel"]
        self.assertEqual(channel["routeId"], "founder@example.com")
        self.assertEqual(channel["consentState"], "pending")
        self.assertIsNotNone(channel["pendingVerification"])

    def test_create_whatsapp_channel_without_template_returns_503(self):
        response = self.client.post(
            reverse("vibe-marketing-notification-channels"),
            {"channelType": "whatsapp", "routeId": "+61400000000"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "whatsapp_not_configured")

    @override_settings(**WHATSAPP_SETTINGS)
    @patch("integrations.services.notification_channels._generate_otp", return_value="654321")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_whatsapp_create_then_verify_endpoint(self, mock_post, _mock_otp):
        mock_post.return_value = _Response(201, {"sid": "SM-1"})
        create = self.client.post(
            reverse("vibe-marketing-notification-channels"),
            {"channelType": "whatsapp", "routeId": "+61 400 111 222"},
            format="json",
        )
        self.assertEqual(create.status_code, status.HTTP_200_OK)
        self.assertEqual(create.data["status"], "otp_sent")
        channel_id = create.data["channel"]["id"]

        bad = self.client.post(
            reverse("vibe-marketing-notification-channel-verify", kwargs={"channel_id": channel_id}),
            {"code": "000000"},
            format="json",
        )
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(bad.data["code"], "invalid_code")

        good = self.client.post(
            reverse("vibe-marketing-notification-channel-verify", kwargs={"channel_id": channel_id}),
            {"code": "654321"},
            format="json",
        )
        self.assertEqual(good.status_code, status.HTTP_200_OK)
        self.assertEqual(good.data["channel"]["consentState"], "active")

    def test_channel_scoped_to_org(self):
        other_org = Organization.objects.create(name="Other", domain="other.example.com")
        foreign = NotificationChannel.objects.create(
            organization=other_org,
            channel_type=NotificationChannelType.EMAIL,
            route_id="other@example.com",
            consent_state=NotificationConsentState.ACTIVE,
        )
        response = self.client.delete(
            reverse("vibe-marketing-notification-channel-detail", kwargs={"channel_id": foreign.id})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_last_channel_pauses_automation_and_legacy_flag(self):
        channel = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.EMAIL,
            route_id="founder@example.com",
            consent_state=NotificationConsentState.ACTIVE,
        )
        ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=channel,
            status=ResearchAutomationStatus.ACTIVE,
        )
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=self.org)
        config.daily_discovery_enabled = True
        config.save(update_fields=["daily_discovery_enabled"])

        response = self.client.delete(
            reverse("vibe-marketing-notification-channel-detail", kwargs={"channel_id": channel.id})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["automationPaused"])
        self.assertEqual(response.data["channel"]["consentState"], "revoked")
        automation = ResearchAutomation.objects.get(organization=self.org)
        self.assertEqual(automation.status, ResearchAutomationStatus.PAUSED)
        config.refresh_from_db()
        self.assertFalse(config.daily_discovery_enabled)

    def test_automation_endpoint_enable_requires_channel(self):
        response = self.client.post(
            reverse("vibe-marketing-notification-automation"),
            {"enabled": True, "timezone": "Australia/Melbourne"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "no_verified_channels")

    def test_automation_endpoint_enable_with_channel_syncs_config(self):
        NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.EMAIL,
            route_id="founder@example.com",
            consent_state=NotificationConsentState.ACTIVE,
        )
        response = self.client.post(
            reverse("vibe-marketing-notification-automation"),
            {"enabled": True, "timezone": "Australia/Melbourne"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["automation"]["enabled"])
        self.assertTrue(response.data["dailyDiscoveryEnabled"])
        config = OrganizationContentConfig.objects.get(organization=self.org)
        self.assertTrue(config.daily_discovery_enabled)

        disable = self.client.post(
            reverse("vibe-marketing-notification-automation"),
            {"enabled": False},
            format="json",
        )
        self.assertEqual(disable.status_code, status.HTTP_200_OK)
        self.assertFalse(disable.data["automation"]["enabled"])
        config.refresh_from_db()
        self.assertFalse(config.daily_discovery_enabled)
