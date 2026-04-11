import uuid

from django.conf import settings
from django.db import models


class Team(models.Model):
    team_id = models.PositiveIntegerField(unique=True, blank=True, null=True)
    team_name = models.CharField(max_length=100)
    avatar_url = models.URLField(blank=True, null=True)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="innovate_connect_alliance_teams",
    )

    def save(self, *args, **kwargs):
        if self.team_id is None:
            last_team = Team.objects.all().order_by("-team_id").first()
            if last_team and last_team.team_id < 100:
                self.team_id = last_team.team_id + 1
            else:
                self.team_id = 1

        if self.team_id < 1 or self.team_id > 100:
            raise ValueError("team_id must be between 1 and 100")

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.team_name} (ID: {self.team_id})"


class Announcement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    body = models.TextField()
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="innovate_connect_alliance_announcements",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class VideoSubmission(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="innovate_connect_alliance_submissions",
    )
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="submissions")
    participant_name = models.CharField(max_length=100)
    title = models.CharField(max_length=255)
    notes = models.TextField(blank=True, null=True)
    video_url = models.URLField()
    storage_path = models.CharField(max_length=500)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    file_size_bytes = models.BigIntegerField()
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"{self.title} by {self.participant_name} at {self.submitted_at}"

