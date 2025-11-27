from django.db import models
from django.conf import settings

class Team(models.Model):
    # team_id is now a positive integer unique field
    team_id = models.PositiveIntegerField(unique=True, blank=True, null=True)
    team_name = models.CharField(max_length=100)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='esafety_teams')

    def save(self, *args, **kwargs):
        if self.team_id is None:
            # Automatically assign next available team_id starting from 1
            last_team = Team.objects.all().order_by('-team_id').first()
            if last_team and last_team.team_id < 100:
                self.team_id = last_team.team_id + 1
            else:
                # If no team exists, assign 1
                self.team_id = 1
        # Validate team_id is between 1 and 100
        if self.team_id < 1 or self.team_id > 100:
            raise ValueError("team_id must be between 1 and 100")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.team_name} (ID: {self.team_id})"

class Submission(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='esafety_submissions')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True)
    file_url = models.URLField(blank=True, null=True) # Assuming file upload or link
    score = models.FloatField(default=0.0)
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Submission by {self.user} at {self.submitted_at}"

import uuid

class Announcement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    body = models.TextField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='esafety_announcements')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title
