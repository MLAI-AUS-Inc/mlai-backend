from django.db import migrations
from django.utils import timezone


def supersede_reopened_attempts(apps, schema_editor):
    Day = apps.get_model("roo", "OfficeManagerDay")
    Assignment = apps.get_model("roo", "OfficeManagerAssignment")
    Attempt = apps.get_model("roo", "OfficeManagerClaimAttempt")

    reopened_day_ids = Assignment.objects.filter(
        status="relinquished",
        day__status="open",
    ).values_list("day_id", flat=True)
    reopened_days = Day.objects.filter(pk__in=reopened_day_ids).values_list(
        "date",
        "generation",
    )
    repaired_at = timezone.now()
    for booking_date, generation in reopened_days.iterator():
        Attempt.objects.filter(
            booking_date=booking_date,
            generation__lt=generation,
        ).exclude(outcome="attempt_superseded").update(
            outcome="attempt_superseded",
            message=(
                "This Office Manager claim attempt was superseded by "
                "cancellation and cannot be replayed"
            ),
            superseded_at=repaired_at,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0038_office_manager_claim_generation"),
    ]

    operations = [
        migrations.RunPython(
            supersede_reopened_attempts,
            noop_reverse,
        ),
    ]
