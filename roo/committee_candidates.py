from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .models import PointsAccount


COMMITTEE_ELIGIBILITY_THRESHOLD = 100
SLACK_PLACEHOLDER_EMAIL_SUFFIX = "@slack.placeholder.com"


class CommitteeCandidateEmailService:
    """Build the private email export for eligible contribution-point members."""

    @classmethod
    def list_emails(cls) -> dict:
        raw_emails = PointsAccount.objects.filter(
            lifetime_earned__gte=COMMITTEE_ELIGIBILITY_THRESHOLD,
            user__is_active=True,
        ).values_list("user__email", flat=True)

        emails = set()
        for raw_email in raw_emails.iterator():
            email = str(raw_email or "").strip().lower()
            if not email or email.endswith(SLACK_PLACEHOLDER_EMAIL_SUFFIX):
                continue
            try:
                validate_email(email)
            except ValidationError:
                continue
            emails.add(email)

        sorted_emails = sorted(emails)
        return {
            "eligible_count": len(sorted_emails),
            "threshold": COMMITTEE_ELIGIBILITY_THRESHOLD,
            "metric": "lifetime_earned",
            "emails": sorted_emails,
        }
