from django.db import models
from django.db.models.functions import Lower
from django.conf import settings
import uuid
from django.utils import timezone


class HospitalCompetitionRound(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_ARCHIVED, 'Archived'),
    ]

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=120)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        db_index=True,
    )
    opened_at = models.DateTimeField(default=timezone.now)
    archived_at = models.DateTimeField(blank=True, null=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='archived_hospital_competition_rounds',
    )
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-opened_at']
        constraints = [
            models.UniqueConstraint(
                fields=['status'],
                condition=models.Q(status='active'),
                name='unique_active_hospital_competition_round',
            ),
        ]

    @classmethod
    def get_active(cls):
        return cls.objects.get(status=cls.STATUS_ACTIVE)

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"


class Team(models.Model):
    round = models.ForeignKey(
        HospitalCompetitionRound,
        on_delete=models.PROTECT,
        related_name='teams',
    )
    team_id = models.PositiveIntegerField(blank=True, null=True)
    team_name = models.CharField(max_length=100)
    avatar_url = models.URLField(blank=True, null=True)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='hospital_teams')

    def save(self, *args, **kwargs):
        if self.round_id is None:
            self.round = HospitalCompetitionRound.get_active()
        if self.team_id is None:
            last_team = Team.objects.filter(round_id=self.round_id).order_by('-team_id').first()
            self.team_id = (last_team.team_id + 1) if last_team else 1
        # Validate team_id is between 1 and 100
        if self.team_id < 1 or self.team_id > 100:
            raise ValueError("team_id must be between 1 and 100")
        super().save(*args, **kwargs)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['round', 'team_id'],
                name='unique_hospital_team_id_per_round',
            ),
            models.UniqueConstraint(
                Lower('team_name'),
                'round',
                name='unique_hospital_team_name_per_round_ci',
            ),
        ]

    def __str__(self):
        return f"{self.team_name} (ID: {self.team_id}, round: {self.round.slug})"

class Announcement(models.Model):
    round = models.ForeignKey(
        HospitalCompetitionRound,
        on_delete=models.PROTECT,
        related_name='announcements',
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    body = models.TextField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hospital_announcements')
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='requested_hospital_announcements',
        null=True,
        blank=True,
    )
    source_channel_id = models.CharField(max_length=32, null=True, blank=True)
    source_message_ts = models.CharField(max_length=32, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.round_id is None:
            self.round = HospitalCompetitionRound.get_active()
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['round', 'source_channel_id', 'source_message_ts'],
                condition=(
                    models.Q(source_channel_id__isnull=False)
                    & models.Q(source_message_ts__isnull=False)
                ),
                name='uniq_hospital_announcement_slack_source_per_round',
            ),
        ]

    def __str__(self):
        return self.title

class Submission(models.Model):
    # Associate a submission with a user and a team (if available)
    round = models.ForeignKey(
        HospitalCompetitionRound,
        on_delete=models.PROTECT,
        related_name='submissions',
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hospital_submissions')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True)
    participant_name = models.CharField(max_length=100)
    score = models.FloatField()
    accuracy = models.FloatField(default=0.0)  # Overall accuracy
    feedback = models.JSONField(null=True, blank=True, help_text="Scoring breakdown: confusion matrix, per-class stats, missed crises, first 100 row details")
    submitted_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.round_id is None:
            if self.team_id is not None:
                self.round_id = self.team.round_id
            else:
                self.round = HospitalCompetitionRound.get_active()
        if self.team_id is not None and self.team.round_id != self.round_id:
            raise ValueError("Submission round must match its team round")
        super().save(*args, **kwargs)

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


class SimParticipant(models.Model):
    """Stable anonymous identity minted by the Health Hack Worker."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Sim Participant"
        verbose_name_plural = "Sim Participants"
        ordering = ['-last_seen_at']

    def __str__(self):
        return str(self.id)


class SimConversation(models.Model):
    """One participant's persisted transcript with one ward NPC."""

    ROLE_PATIENT = 'patient'
    ROLE_NURSE = 'nurse'
    ROLE_CLERK = 'clerk'
    ROLE_CHOICES = [
        (ROLE_PATIENT, 'Sash'),
        (ROLE_NURSE, 'Dr Snow'),
        (ROLE_CLERK, 'Nurse Paws'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    participant = models.ForeignKey(
        SimParticipant,
        on_delete=models.CASCADE,
        related_name='conversations',
    )
    case_id = models.PositiveIntegerField(db_index=True)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    last_turn_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_turn_at']
        constraints = [
            models.UniqueConstraint(
                fields=['participant', 'case_id', 'role'],
                name='uniq_sim_conversation_participant_case_role',
            ),
        ]

    def __str__(self):
        return f"{self.participant_id} · case {self.case_id} · {self.role}"


class SimConversationTurn(models.Model):
    """A single player question and its resulting NPC response or error."""

    SOURCE_PENDING = 'pending'
    SOURCE_LLM = 'llm'
    SOURCE_DETERMINISTIC = 'deterministic'
    SOURCE_ERROR = 'error'
    SOURCE_CHOICES = [
        (SOURCE_PENDING, 'Pending'),
        (SOURCE_LLM, 'LLM'),
        (SOURCE_DETERMINISTIC, 'Deterministic'),
        (SOURCE_ERROR, 'Error'),
    ]

    conversation = models.ForeignKey(
        SimConversation,
        on_delete=models.CASCADE,
        related_name='turns',
    )
    message_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    player_text = models.TextField()
    npc_text = models.TextField(blank=True, default='')
    response_source = models.CharField(
        max_length=16,
        choices=SOURCE_CHOICES,
        default=SOURCE_PENDING,
    )
    model_name = models.CharField(max_length=100, blank=True, default='')
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    tool_calls = models.JSONField(default=list, blank=True)
    suggested_action = models.JSONField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Exact, strictly projected HTTP envelope used for idempotent replay. This
    # never stores Roo's internal case metadata or tool traces.
    public_response = models.JSONField(null=True, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['created_at'], name='sim_turn_created_idx'),
        ]

    def __str__(self):
        return f"{self.conversation_id} · {self.response_source} · {self.created_at:%Y-%m-%d %H:%M}"


class SimDiagnosisGuess(models.Model):
    """
    One web-game diagnosis guess per anonymous browser client per case.

    Web ward contest only — deliberately NOT related to the Slack medhack
    tables (MedHackCase/Guess/Winner). case_id mirrors roo cases.yaml ids.
    Correctness is adjudicated by Roo (same fuzzy matcher as the Slack game);
    the backend records the verdict and owns the prize state.
    """
    OUTCOME_PENDING_CLAIM = 'pending_claim'
    OUTCOME_INCORRECT = 'incorrect'
    OUTCOME_TICKET = 'ticket'
    OUTCOME_DISCOUNT = 'discount'
    OUTCOME_CHOICES = [
        (OUTCOME_PENDING_CLAIM, 'Correct — awaiting email'),
        (OUTCOME_INCORRECT, 'Incorrect'),
        (OUTCOME_TICKET, 'Free ticket (winner)'),
        (OUTCOME_DISCOUNT, '30% discount'),
    ]

    PRIZE_NONE = 'none'
    PRIZE_FREE_TICKET = 'free_ticket'
    PRIZE_DISCOUNT_30 = 'discount_30'
    PRIZE_CHOICES = [
        (PRIZE_NONE, 'No prize'),
        (PRIZE_FREE_TICKET, 'Free ticket'),
        (PRIZE_DISCOUNT_30, '30% discount'),
    ]

    case_id = models.PositiveIntegerField(db_index=True, help_text="Case ID from roo cases.yaml")
    case_title = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Human-readable simulated-patient challenge title from Roo",
    )
    client_id = models.CharField(max_length=64, help_text="Anonymous browser UUID")
    participant = models.ForeignKey(
        SimParticipant,
        on_delete=models.PROTECT,
        related_name='guesses',
        null=True,
        blank=True,
    )
    guess_text = models.CharField(max_length=300)
    is_correct = models.BooleanField()
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES)
    prize_kind = models.CharField(
        max_length=20,
        choices=PRIZE_CHOICES,
        default=PRIZE_NONE,
    )
    email = models.EmailField(
        blank=True,
        default="",
        help_text="Prize registration email; stored only, no email is sent",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    redemption_delivered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Legacy field; unused by the link-only prize flow",
    )

    class Meta:
        verbose_name = "Sim Diagnosis Guess"
        verbose_name_plural = "Sim Diagnosis Guesses"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['case_id', 'client_id'],
                name='uniq_sim_guess_case_client',
            ),
            models.UniqueConstraint(
                Lower('email'),
                'case_id',
                condition=~models.Q(email=''),
                name='uniq_sim_claim_email_case_ci',
            ),
        ]

    def __str__(self):
        return f"{self.client_id[:8]}… on case {self.case_id}: {self.outcome}"


class SimCaseWinner(models.Model):
    """
    The single free-ticket winner per web-contest case.
    unique=True on case_id IS the contest lock. The winner slot is claimed at
    correct-guess recording time; a concurrent later solver falls through to
    the discount before either player reaches the email-claim screen.
    """
    case_id = models.PositiveIntegerField(unique=True)
    guess = models.OneToOneField(SimDiagnosisGuess, on_delete=models.PROTECT, related_name='win')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Sim Case Winner"
        verbose_name_plural = "Sim Case Winners"
        ordering = ['-created_at']

    def __str__(self):
        return f"Case {self.case_id} won by {self.guess.email or self.guess.client_id[:8]}"
