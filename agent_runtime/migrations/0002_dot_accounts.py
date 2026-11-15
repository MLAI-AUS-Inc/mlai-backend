# Approved by the owner on 15 Nov 2026 for Slack-style Dot accounts:
# a community-unique handle, About text, and a Suspended state.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent_runtime", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentinstallation",
            name="handle",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="agentinstallation",
            name="about",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="agentinstallation",
            name="suspended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="agentinstallation",
            name="state",
            field=models.CharField(
                choices=[
                    ("awaiting_owner_proof", "awaiting_owner_proof"),
                    ("provisioning", "provisioning"),
                    ("active", "active"),
                    ("paused", "paused"),
                    ("suspended", "suspended"),
                    ("revoked", "revoked"),
                ],
                default="active",
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="agentinstallation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("handle__isnull", False)),
                fields=("community_id", "handle"),
                name="agent_installation_unique_handle",
            ),
        ),
    ]
