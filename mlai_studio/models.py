from django.db import models


class StudioApplication(models.Model):
    """A developer signup/enquiry from the public MLAI Studio landing page.

    Rows are captured progressively: a ``lead`` row is created as soon as the
    applicant finishes step 1 (name/email/phone), then upgraded to
    ``complete`` when the full form is submitted. ``client_ref`` is the
    browser-generated id the form sends with both saves, so the submit
    endpoint can upsert instead of duplicating.

    Applications include work-eligibility/visa details, so they are only ever
    read through the Django admin — there is no public read API.
    """

    STAGE_LEAD = 'lead'
    STAGE_COMPLETE = 'complete'
    STAGE_CHOICES = (
        (STAGE_LEAD, 'Lead'),
        (STAGE_COMPLETE, 'Complete'),
    )

    client_ref = models.CharField(max_length=64, unique=True)
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES, default=STAGE_LEAD)

    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=64, blank=True)

    location = models.CharField(max_length=255, blank=True)
    legal_work = models.CharField(max_length=64, blank=True)
    visa = models.CharField(max_length=255, blank=True)

    linkedin = models.CharField(max_length=500, blank=True)
    github = models.CharField(max_length=500, blank=True)
    portfolio = models.CharField(max_length=500, blank=True)

    skills = models.JSONField(default=list, blank=True)
    skills_other = models.JSONField(default=list, blank=True)
    ai_tools = models.JSONField(default=list, blank=True)
    ai_tools_other = models.JSONField(default=list, blank=True)
    interests = models.JSONField(default=list, blank=True)
    interests_other = models.JSONField(default=list, blank=True)

    availability = models.CharField(max_length=64, blank=True)
    availability_other = models.CharField(max_length=255, blank=True)
    # Choice labels from the form ("Right now", "Within 2 weeks"), not dates.
    start_date = models.CharField(max_length=64, blank=True)
    start_date_other = models.CharField(max_length=255, blank=True)
    rate = models.CharField(max_length=255, blank=True)

    projects = models.TextField(blank=True)
    anything_else = models.TextField(blank=True)
    consent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.full_name} <{self.email}> ({self.stage})'
