from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("integrations", "0050_slack_member_channels")]

    operations = [
        migrations.AddField(
            model_name="googleconnection",
            name="provider_account_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.RemoveConstraint(
            model_name="googleconnection",
            name="uniq_google_connection_user_org",
        ),
        migrations.AddConstraint(
            model_name="googleconnection",
            constraint=models.UniqueConstraint(
                fields=("user", "organization", "provider_account_id"),
                name="uniq_google_user_org_account",
                nulls_distinct=False,
            ),
        ),
    ]
