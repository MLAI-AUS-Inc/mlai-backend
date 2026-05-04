from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Announcement, Team, VideoSubmission

User = get_user_model()


class InnovateConnectAllianceApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = self._make_user(
            email="captain@example.com",
            first_name="Casey",
            last_name="Captain",
        )
        self.client.force_authenticate(self.user)

    def _make_user(self, *, email, first_name="Test", last_name="User", role="participant"):
        return User.objects.create_user(
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=role,
        )

    def _make_team(self, *, team_id, team_name, members=None, avatar_url=None):
        team = Team.objects.create(
            team_id=team_id,
            team_name=team_name,
            avatar_url=avatar_url,
        )
        if members:
            team.members.add(*members)
        return team

    def _valid_team(self, *, team_id=7, team_name="Alliance Builders"):
        teammate = self._make_user(
            email=f"teammate-{team_id}@example.com",
            first_name="Terry",
            last_name="Teammate",
        )
        return self._make_team(
            team_id=team_id,
            team_name=team_name,
            members=[self.user, teammate],
        )

    def test_auth_me_returns_innovate_connect_alliance_team(self):
        team = self._valid_team(team_id=12, team_name="Video Pioneers")

        response = self.client.get("/api/v1/auth/me/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["team"]["team_id"], team.team_id)
        self.assertEqual(
            response.data["innovate_connect_alliance_team"]["team_name"],
            "Video Pioneers",
        )
        self.assertEqual(response.data["innovate_connect_alliance_team"]["member_count"], 2)
        self.assertTrue(response.data["innovate_connect_alliance_team"]["is_valid_team_size"])
        self.assertEqual(len(response.data["innovate_connect_alliance_team"]["members"]), 2)

    def test_create_team_assigns_user_and_member_filter_returns_team(self):
        response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/teams/",
            {"team_name": "Future Makers"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["created"])
        self.assertEqual(response.data["team"]["team_name"], "Future Makers")
        self.assertEqual(response.data["team"]["code"], "TEAM1")
        self.assertEqual(response.data["team"]["member_count"], 1)
        self.assertEqual(response.data["team"]["members"][0]["email"], self.user.email)
        self.assertEqual(response.data["team"]["members"][0]["role"], "participant")

        self.user.refresh_from_db()
        self.assertTrue(Team.objects.filter(members=self.user).exists())

        auth_response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(auth_response.status_code, 200)
        self.assertTrue(auth_response.data["has_team"])

        list_response = self.client.get("/api/v1/hackathons/innovate-connect-alliance/teams/")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data[0]["team_name"], "Future Makers")
        self.assertEqual(list_response.data[0]["members"][0]["email"], self.user.email)

        member_response = self.client.get(
            f"/api/v1/hackathons/innovate-connect-alliance/teams/?member_id={self.user.id}"
        )
        self.assertEqual(member_response.status_code, 200)
        self.assertEqual(len(member_response.data), 1)
        self.assertEqual(member_response.data[0]["team_name"], "Future Makers")

    def test_join_team_by_code_switches_user_from_previous_team(self):
        original_team = self._make_team(
            team_id=1,
            team_name="Original Team",
            members=[self.user],
        )
        teammate = self._make_user(email="partner@example.com")
        target_team = self._make_team(
            team_id=42,
            team_name="Target Team",
            members=[teammate],
        )

        response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/teams/join/",
            {"code": "TEAM42"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["team_id"], 42)
        self.assertEqual(response.data["team_name"], "Target Team")
        self.assertFalse(original_team.members.filter(id=self.user.id).exists())
        self.assertTrue(target_team.members.filter(id=self.user.id).exists())

    def test_join_team_by_id_switches_user_from_previous_team(self):
        original_team = self._make_team(
            team_id=3,
            team_name="Alpha Team",
            members=[self.user],
        )
        teammate = self._make_user(email="partner-two@example.com")
        target_team = self._make_team(
            team_id=8,
            team_name="Beta Team",
            members=[teammate],
        )

        response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/teams/join/",
            {"team_id": 8},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(original_team.members.filter(id=self.user.id).exists())
        self.assertTrue(target_team.members.filter(id=self.user.id).exists())
        self.assertEqual(response.data["code"], "TEAM8")

    def test_create_existing_full_team_does_not_remove_user_from_current_team(self):
        current_team = self._make_team(
            team_id=5,
            team_name="Current Team",
            members=[self.user],
        )
        full_members = [
            self._make_user(email=f"full-{index}@example.com")
            for index in range(6)
        ]
        full_team = self._make_team(
            team_id=6,
            team_name="Full Team",
            members=full_members,
        )

        response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/teams/",
            {"team_name": "Full Team"},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(current_team.members.filter(id=self.user.id).exists())
        self.assertFalse(full_team.members.filter(id=self.user.id).exists())

    @patch("innovate_connect_alliance.views.upload_file_to_storage")
    def test_submission_upload_succeeds_and_history_endpoints_match(self, mock_upload):
        mock_upload.return_value = "https://storage.example.com/submissions/demo.mp4"
        team = self._valid_team(team_id=9, team_name="Pitch Masters")
        video = SimpleUploadedFile("demo.mp4", b"video-bytes", content_type="video/mp4")

        create_response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/submissions/",
            {
                "title": "Launch Video",
                "notes": "First cut",
                "video": video,
            },
            format="multipart",
        )

        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data["title"], "Launch Video")
        self.assertEqual(create_response.data["participant_name"], self.user.full_name)
        self.assertEqual(create_response.data["team"]["team_id"], team.team_id)
        self.assertEqual(create_response.data["team"]["code"], "TEAM9")
        submission_id = create_response.data["submission_id"]

        latest_response = self.client.get(
            "/api/v1/hackathons/innovate-connect-alliance/submissions/latest/"
        )
        self.assertEqual(latest_response.status_code, 200)
        self.assertEqual(latest_response.data["submission_id"], submission_id)

        recent_response = self.client.get(
            "/api/v1/hackathons/innovate-connect-alliance/submissions/recent/"
        )
        self.assertEqual(recent_response.status_code, 200)
        self.assertEqual(len(recent_response.data), 1)
        self.assertEqual(recent_response.data[0]["submission_id"], submission_id)

        list_response = self.client.get(
            "/api/v1/hackathons/innovate-connect-alliance/submissions/"
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.data), 1)
        self.assertEqual(list_response.data[0]["submission_id"], submission_id)

        detail_response = self.client.get(
            f"/api/v1/hackathons/innovate-connect-alliance/submissions/{submission_id}/"
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.data["video_url"], mock_upload.return_value)

        self.assertEqual(VideoSubmission.objects.count(), 1)
        mock_upload.assert_called_once()

    def test_submission_rejects_missing_video(self):
        self._valid_team(team_id=10, team_name="Upload Ready")

        response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/submissions/",
            {"title": "Missing File"},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "video is required")

    def test_submission_rejects_non_video_content(self):
        self._valid_team(team_id=11, team_name="Upload Ready")
        not_video = SimpleUploadedFile("notes.txt", b"plain-text", content_type="text/plain")

        response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/submissions/",
            {"title": "Wrong Format", "video": not_video},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Uploaded file must be a video.")

    @patch("innovate_connect_alliance.views.MAX_VIDEO_SIZE_BYTES", 1024 * 1024)
    def test_submission_rejects_oversized_video(self):
        self._valid_team(team_id=13, team_name="Upload Ready")
        oversized_video = SimpleUploadedFile(
            "oversized.mp4",
            b"a" * ((1024 * 1024) + 1),
            content_type="video/mp4",
        )

        response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/submissions/",
            {"title": "Too Large", "video": oversized_video},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("maximum size", response.data["detail"])

    def test_submission_rejects_invalid_team_size(self):
        self._make_team(team_id=14, team_name="Solo Builders", members=[self.user])
        video = SimpleUploadedFile("demo.mp4", b"video-bytes", content_type="video/mp4")

        response = self.client.post(
            "/api/v1/hackathons/innovate-connect-alliance/submissions/",
            {"title": "Needs Team", "video": video},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["team_id"], 14)
        self.assertEqual(response.data["member_count"], 1)

    def test_announcements_endpoint_returns_announcements(self):
        author = self._make_user(email="announcer@example.com", role="admin")
        Announcement.objects.create(
            title="Submissions Open",
            body="Upload your team video before the deadline.",
            author=author,
        )

        response = self.client.get("/api/v1/hackathons/innovate-connect-alliance/announcements/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Submissions Open")
