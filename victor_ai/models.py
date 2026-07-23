from django.db import models


class VictorApplication(models.Model):
    """A registration from the public Victor:AI landing page (victorai.win).

    Rows are captured progressively: a ``lead`` row is created as soon as the
    applicant finishes step 1 (first name / last name / email), then upgraded
    to ``complete`` when the full form is submitted. ``client_ref`` is the
    browser-generated id the form sends with both saves, so the submit
    endpoint can upsert instead of duplicating.

    Registrations are only ever read through the Django admin — there is no
    public read API.
    """

    STAGE_LEAD = 'lead'
    STAGE_COMPLETE = 'complete'
    STAGE_CHOICES = (
        (STAGE_LEAD, 'Lead'),
        (STAGE_COMPLETE, 'Complete'),
    )

    client_ref = models.CharField(max_length=64, unique=True)
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES, default=STAGE_LEAD)

    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255)
    # Not unique: people can register twice; client_ref is the upsert key.
    email = models.EmailField(db_index=True)
    # Applicant's own LinkedIn (optional); captured in step 1 alongside contact.
    linkedin = models.CharField(max_length=500, blank=True)

    team_name = models.CharField(max_length=255, blank=True)
    # Choice labels from the form ("Founder", "Idea stage"), not enums, so
    # the form options can change without a migration.
    role = models.CharField(max_length=64, blank=True)
    startup_stage = models.CharField(max_length=64, blank=True)
    industry_sector = models.CharField(max_length=64, blank=True)
    location = models.CharField(max_length=255, blank=True)

    # Team composition: total headcount including the applicant, plus
    # first/last/email for each *other* member (list of dicts).
    team_size = models.PositiveIntegerField(null=True, blank=True)
    team_members = models.JSONField(default=list, blank=True)
    # Monthly revenue (AUD) for the last three months, keyed 'YYYY-MM'.
    # Only collected when startup_stage indicates paying users or funding.
    revenue_last_3_months = models.JSONField(default=dict, blank=True)

    idea = models.TextField(blank=True)
    support = models.TextField(blank=True)
    consent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.first_name} {self.last_name} <{self.email}> ({self.stage})'


class VictorRooRequestReceipt(models.Model):
    """One-time nonce receipt for signed Roo application-data requests."""

    nonce = models.CharField(max_length=128, unique=True)
    request_id = models.CharField(max_length=128, db_index=True)
    event_id = models.CharField(max_length=128, blank=True, default='')
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class VictorApplicationAccessAudit(models.Model):
    """PII-free audit metadata for every authorised Victor read/export."""

    action = models.CharField(max_length=32, db_index=True)
    slack_team_id = models.CharField(max_length=32, db_index=True)
    slack_channel_id = models.CharField(max_length=32, db_index=True)
    acting_slack_user_id = models.CharField(max_length=32, db_index=True)
    request_id = models.CharField(max_length=128, db_index=True)
    target_application_id = models.PositiveBigIntegerField(null=True, blank=True)
    filters = models.JSONField(default=dict, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    outcome = models.CharField(max_length=32, default='success')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
