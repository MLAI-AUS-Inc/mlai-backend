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


class MedHackCase(models.Model):
    """
    Tracks activation of a MedHack diagnosis game case.
    Case content (patient data, answers) lives in Roo's local YAML files.
    This model only tracks which cases have been played and their game state.
    """
    case_id = models.IntegerField(help_text="Case ID from cases.yaml (not unique — same case can be replayed)")
    is_active = models.BooleanField(default=True)
    solved = models.BooleanField(default=False, help_text="Whether the case has been correctly diagnosed")
    hint_level = models.IntegerField(default=0, help_text="Current hint level for progressive hints")
    started_by_slack_id = models.CharField(max_length=50, help_text="Admin Slack ID who started this case")
    started_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = "MedHack Case"
        verbose_name_plural = "MedHack Cases"
        ordering = ['-started_at']
        db_table = 'roo_medhackcase'

    def __str__(self):
        status = "ACTIVE" if self.is_active else "closed"
        return f"Case {self.case_id} (#{self.id}) - {status}"


class MedHackGuess(models.Model):
    """
    Per-user guess records for a MedHack case.
    Max 1 confirmed guess per user per case.
    Guess correctness is determined by Roo (fuzzy match) — backend just records the result.
    """
    case = models.ForeignKey(MedHackCase, on_delete=models.CASCADE, related_name='guesses')
    slack_user_id = models.CharField(max_length=50)
    guess = models.TextField()
    correct = models.BooleanField(null=True, blank=True, help_text="Null while pending, True/False after confirmation")
    is_pending = models.BooleanField(default=True, help_text="True if awaiting user confirmation")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = "MedHack Guess"
        verbose_name_plural = "MedHack Guesses"
        ordering = ['-created_at']
        db_table = 'roo_medhackguess'
        indexes = [
            models.Index(fields=['case', 'slack_user_id']),
        ]

    def __str__(self):
        status = "pending" if self.is_pending else ("correct" if self.correct else "incorrect")
        return f"{self.slack_user_id} on Case #{self.case_id} - {status}"


class MedHackWinner(models.Model):
    """
    Winner records for MedHack cases.
    """
    case = models.ForeignKey(MedHackCase, on_delete=models.CASCADE, related_name='winners')
    slack_user_id = models.CharField(max_length=50)
    is_first_solver = models.BooleanField(default=False)
    won_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "MedHack Winner"
        verbose_name_plural = "MedHack Winners"
        unique_together = ('case', 'slack_user_id')
        ordering = ['-won_at']
        db_table = 'roo_medhackwinner'

    def __str__(self):
        first = " (FIRST!)" if self.is_first_solver else ""
        return f"{self.slack_user_id} won Case #{self.case_id}{first}"
