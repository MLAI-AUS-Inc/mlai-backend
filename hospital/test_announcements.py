from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Announcement, HospitalCompetitionRound


User = get_user_model()


@override_settings(
    ROO_API_KEY="roo-test-key",
    INTERNAL_API_KEY="roo-test-key",
    HEALTHHACK_ANNOUNCEMENT_ADMIN_IDS=[],
)
class HealthHackAnnouncementTests(TestCase):
    canonical_url = "/api/v1/hackathons/hospital/announcements/"
    legacy_url = "/api/v1/medhack/announcements/"

    def setUp(self):
        self.client = APIClient()
        self.organiser = User.objects.create_user(
            email="organiser@example.com",
            first_name="HealthHack",
            last_name="Organiser",
        )
        self.organiser.is_superuser = True
        self.organiser.slack_id = "U0SUPER123"
        self.organiser.save(update_fields=["is_superuser", "slack_id"])

        self.participant = User.objects.create_user(
            email="participant@example.com",
            first_name="HealthHack",
            last_name="Participant",
        )
        self.participant.slack_id = "U0PART1234"
        self.participant.save(update_fields=["slack_id"])

        self.bot = User.objects.create_user(email="roo@example.com", first_name="Roo")
        self.bot.slack_id = "U0ROO00000"
        self.bot.save(update_fields=["slack_id"])

    def payload(self, **overrides):
        payload = {
            "title": "Doors open",
            "body": "Registration opens at 10:30am.",
            "requester_slack_id": self.organiser.slack_id,
            "author_slack_id": self.bot.slack_id,
            "source_channel_id": "C0BHZ9NS21L",
            "source_message_ts": "1784286514.495879",
        }
        payload.update(overrides)
        return payload

    def post(self, payload=None, *, url=None, include_key=True):
        headers = {"HTTP_X_API_KEY": "roo-test-key"} if include_key else {}
        return self.client.post(
            url or self.canonical_url,
            payload or self.payload(),
            format="json",
            **headers,
        )

    def test_superuser_can_publish_bot_authored_announcement(self):
        response = self.post()

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["created"])
        announcement = Announcement.objects.get()
        self.assertEqual(announcement.round, HospitalCompetitionRound.get_active())
        self.assertEqual(announcement.author, self.bot)
        self.assertEqual(announcement.requester, self.organiser)
        self.assertEqual(announcement.source_channel_id, "C0BHZ9NS21L")
        self.assertEqual(announcement.source_message_ts, "1784286514.495879")

    def test_missing_service_key_is_rejected(self):
        response = self.post(include_key=False)

        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Announcement.objects.exists())

    def test_non_organiser_is_forbidden(self):
        response = self.post(self.payload(requester_slack_id=self.participant.slack_id))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Announcement.objects.exists())

    @override_settings(HEALTHHACK_ANNOUNCEMENT_ADMIN_IDS=["U0PART1234"])
    def test_explicit_active_organiser_allowlist_is_supported(self):
        response = self.post(self.payload(requester_slack_id=self.participant.slack_id))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Announcement.objects.get().requester, self.participant)

    def test_same_slack_message_is_an_idempotent_replay(self):
        first = self.post()
        replay = self.post()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertFalse(replay.data["created"])
        self.assertEqual(Announcement.objects.count(), 1)

    def test_same_slack_message_cannot_be_overwritten(self):
        self.assertEqual(self.post().status_code, 201)

        conflict = self.post(self.payload(body="Different content"))

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(Announcement.objects.count(), 1)
        self.assertEqual(Announcement.objects.get().body, "Registration opens at 10:30am.")

    def test_source_channel_and_timestamp_must_be_supplied_together(self):
        response = self.post(self.payload(source_message_ts=None))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Announcement.objects.exists())

    def test_participant_cannot_list_roo_published_announcements(self):
        self.assertEqual(self.post().status_code, 201)
        viewer = APIClient()
        viewer.force_authenticate(user=self.participant)

        response = viewer.get(self.canonical_url)

        self.assertEqual(response.status_code, 403)

    def test_superuser_can_list_roo_published_announcement(self):
        self.assertEqual(self.post().status_code, 201)
        viewer = APIClient()
        viewer.force_authenticate(user=self.organiser)

        response = viewer.get(self.canonical_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["title"], "Doors open")
        self.assertEqual(response.data[0]["body"], "Registration opens at 10:30am.")
        self.assertEqual(response.data[0]["author"]["name"], self.bot.full_name)

    def test_legacy_medhack_url_uses_same_secured_create_flow(self):
        response = self.post(url=self.legacy_url)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Announcement.objects.count(), 1)
        self.assertEqual(Announcement.objects.get().requester, self.organiser)
