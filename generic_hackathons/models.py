from django.conf import settings
from django.db import models


class GenericHackathonTeam(models.Model):
    hackathon = models.ForeignKey(
        'core.Hackathon',
        on_delete=models.CASCADE,
        related_name='generic_teams',
    )
    team_id = models.PositiveIntegerField(blank=True, null=True)
    team_name = models.CharField(max_length=120)
    avatar_url = models.URLField(blank=True, null=True)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='generic_hackathon_teams',
        blank=True,
    )
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='led_generic_hackathon_teams',
        null=True,
        blank=True,
    )
    # FastAPI eval-server credentials, populated by the
    # `approve_teams_for_eval` admin action. Together they form the auth pair
    # participants paste into the WTH submission portal — neither value alone
    # lets a team submit, so both are populated atomically or not at all.
    eval_token = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Plaintext token issued by the eval gateway (stored in teams.token_hash on the cluster).",
    )
    eval_team_uuid = models.UUIDField(
        blank=True,
        null=True,
        unique=True,
        help_text="teams.id UUID from the eval gateway. Participants paste this into the portal Team ID field.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['team_id', 'team_name']
        constraints = [
            models.UniqueConstraint(
                fields=['hackathon', 'team_id'],
                name='unique_generic_hackathon_team_id',
            ),
            models.UniqueConstraint(
                fields=['hackathon', 'team_name'],
                name='unique_generic_hackathon_team_name',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.team_id is None:
            last_team = (
                GenericHackathonTeam.objects
                .filter(hackathon=self.hackathon)
                .order_by('-team_id')
                .first()
            )
            self.team_id = (last_team.team_id + 1) if last_team and last_team.team_id else 1
        super().save(*args, **kwargs)

    @property
    def code(self):
        return f"TEAM{self.team_id}" if self.team_id is not None else None

    def __str__(self):
        return f"{self.team_name} ({self.hackathon.slug})"


class GenericHackathonJoinRequest(models.Model):
    """A pending request to join a team. The leader accepts (-> member) or rejects/cancels (-> row
    deleted), so an existing row always represents a *pending* request."""
    team = models.ForeignKey(
        GenericHackathonTeam,
        on_delete=models.CASCADE,
        related_name='join_requests',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='generic_hackathon_join_requests',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'user'],
                name='unique_generic_hackathon_join_request',
            ),
        ]

    def __str__(self):
        return f"{self.user} -> {self.team.team_name}"


class GenericHackathonSubmission(models.Model):
    hackathon = models.ForeignKey(
        'core.Hackathon',
        on_delete=models.CASCADE,
        related_name='generic_submissions',
    )
    team = models.ForeignKey(
        GenericHackathonTeam,
        on_delete=models.CASCADE,
        related_name='submissions',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='generic_hackathon_submissions',
    )
    title = models.CharField(max_length=180)
    summary = models.TextField()
    repository_url = models.URLField(blank=True, null=True)
    demo_url = models.URLField(blank=True, null=True)
    slides_url = models.URLField(blank=True, null=True)
    attachment_url = models.URLField(blank=True, null=True)
    attachment_name = models.CharField(max_length=255, blank=True)
    attachment_content_type = models.CharField(max_length=120, blank=True)
    attachment_size = models.PositiveIntegerField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.hackathon.slug})"


class GenericHackathonAnnouncement(models.Model):
    hackathon = models.ForeignKey(
        'core.Hackathon',
        on_delete=models.CASCADE,
        related_name='generic_announcements',
    )
    title = models.CharField(max_length=255)
    body = models.TextField()
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='generic_hackathon_announcements',
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class GenericHackathonResource(models.Model):
    hackathon = models.ForeignKey(
        'core.Hackathon',
        on_delete=models.CASCADE,
        related_name='generic_resources',
    )
    title = models.CharField(max_length=180)
    summary = models.TextField()
    body = models.TextField(blank=True)
    url = models.URLField(blank=True, null=True)
    category = models.CharField(max_length=80, blank=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'title']

    def __str__(self):
        return f"{self.title} ({self.hackathon.slug})"


class WattTheHackSettings(models.Model):
    unlocked_scenarios = models.CharField(
        max_length=2000,
        default="t1_welcome,t2_first_code",
        help_text="Comma-separated list of unlocked scenario IDs (e.g. 't1_welcome,t2_first_code,s1_duck_curve')",
    )
    auto_unlock = models.BooleanField(
        default=True,
        help_text="If checked, all scenarios NOT marked as judging will be unlocked automatically.",
    )
    require_team_for_sim = models.BooleanField(
        default=False,
        help_text="If checked, users must be in a WTH team to use the sandbox.",
    )
    
    class Meta:
        verbose_name = "Watt The Hack Settings"
        verbose_name_plural = "Watt The Hack Settings"

    def __str__(self):
        return "Watt The Hack Settings"

    @classmethod
    def get_settings(cls):
        obj, created = cls.objects.get_or_create(id=1)
        return obj
