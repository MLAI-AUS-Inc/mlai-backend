from django.db import migrations


def clear_synthetic_web_slack_ids(apps, schema_editor):
    User = apps.get_model("core", "User")

    candidates = User.objects.filter(slack_id__startswith="web_").only(
        "id",
        "slack_id",
    )
    for user in candidates.iterator():
        expected_value = f"web_{user.pk}"
        if user.slack_id == expected_value:
            User.objects.filter(
                pk=user.pk,
                slack_id=expected_value,
            ).update(slack_id=None)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0060_slackfounderlinkrequest_consumed_by_user"),
    ]

    operations = [
        migrations.RunPython(
            clear_synthetic_web_slack_ids,
            migrations.RunPython.noop,
        ),
    ]
