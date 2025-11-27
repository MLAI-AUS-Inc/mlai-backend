from django.db import models
from django.conf import settings
import uuid
from django.utils import timezone

class Team(models.Model):
    # team_id is now a positive integer unique field
    team_id = models.PositiveIntegerField(unique=True, blank=True, null=True)
    team_name = models.CharField(max_length=100)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='hospital_teams')

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

class Announcement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    body = models.TextField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hospital_announcements')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

class Submission(models.Model):
    # Associate a submission with a user and a team (if available)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hospital_submissions')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True)
    participant_name = models.CharField(max_length=100)
    score = models.FloatField()
    accuracy = models.FloatField(default=0.0)  # Overall accuracy
    submitted_at = models.DateTimeField(auto_now_add=True)

class Prediction(models.Model):
    submission = models.ForeignKey(Submission, related_name='predictions', on_delete=models.CASCADE)
    row_id = models.IntegerField()  # row number (order in the CSV)
    predicted_label = models.IntegerField()
    correct_label = models.IntegerField()
    timestamp = models.DateTimeField(null=True, blank=True)
    diastolic_bp = models.FloatField(null=True, blank=True)
    systolic_bp = models.FloatField(null=True, blank=True)
    heart_rate = models.FloatField(null=True, blank=True)
    respiratory_rate = models.FloatField(null=True, blank=True)
    oxygen_saturation = models.FloatField(null=True, blank=True)
