from django.db import migrations, models


def backfill_claimed_points_snapshot(apps, schema_editor):
    TaskAssignment = apps.get_model("roo", "TaskAssignment")

    for assignment in TaskAssignment.objects.select_related("task").all().iterator():
        if assignment.claimed_points_snapshot is not None:
            continue

        task = assignment.task
        assignment.claimed_points_snapshot = (
            assignment.awarded_points
            or getattr(task, "points_estimate", None)
            or task.points
        )
        assignment.save(update_fields=["claimed_points_snapshot"])


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0019_structured_task_engine"),
    ]

    operations = [
        migrations.AddField(
            model_name="taskassignment",
            name="claimed_points_snapshot",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_claimed_points_snapshot,
            migrations.RunPython.noop,
        ),
    ]
