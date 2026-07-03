from django.db import migrations
from django.db.models import F


def backfill_ready_at(apps, schema_editor):
    """Backfill ready_at for drafts that reached 'ready' before the field existed.

    We have no historical record of the actual transition time, so use
    updated_at as the best available proxy: a draft that reached 'ready' and
    was not touched since has updated_at ~= the transition time. Where the row
    was saved again later this over-states recency slightly, which errs in the
    founder's favour for time-based perks.
    """
    MonthlyUpdateDraft = apps.get_model("startup_updates", "MonthlyUpdateDraft")
    MonthlyUpdateDraft.objects.filter(
        status="ready", ready_at__isnull=True
    ).update(ready_at=F("updated_at"))


def clear_ready_at(apps, schema_editor):
    MonthlyUpdateDraft = apps.get_model("startup_updates", "MonthlyUpdateDraft")
    MonthlyUpdateDraft.objects.update(ready_at=None)


class Migration(migrations.Migration):
    dependencies = [
        ("startup_updates", "0014_monthlyupdatedraft_ready_at"),
    ]

    operations = [
        migrations.RunPython(backfill_ready_at, clear_ready_at),
    ]
