from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_consumed_by_user(apps, schema_editor):
    Link = apps.get_model("core", "SlackFounderAccountLink")
    LinkRequest = apps.get_model("core", "SlackFounderLinkRequest")

    consumed_requests = LinkRequest.objects.filter(
        consumed_at__isnull=False,
        consumed_by_user__isnull=True,
    ).iterator()
    for request in consumed_requests:
        founder_user_id = (
            Link.objects.filter(slack_user_id=request.slack_user_id)
            .values_list("founder_user_id", flat=True)
            .first()
        )
        request.consumed_by_user_id = founder_user_id or request.slack_user_id
        request.save(update_fields=["consumed_by_user"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0059_slackfounderaccountlink_distinct_users"),
    ]

    operations = [
        migrations.AddField(
            model_name="slackfounderlinkrequest",
            name="consumed_by_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="consumed_slack_founder_link_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(
            backfill_consumed_by_user,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="slackfounderlinkrequest",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        consumed_at__isnull=True,
                        consumed_by_user__isnull=True,
                    )
                    | models.Q(
                        consumed_at__isnull=False,
                        consumed_by_user__isnull=False,
                    )
                ),
                name="core_sflr_consumed_actor",
            ),
        ),
    ]
