from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0035_generic_community_bridge_destination"),
    ]

    operations = [
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_workspace_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=100),
        ),
        migrations.CreateModel(
            name="CommunityBridgeIdentityLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_workspace_id", models.CharField(max_length=100)),
                ("slack_user_id", models.CharField(max_length=100)),
                (
                    "buzz_pubkey",
                    models.CharField(
                        max_length=64,
                        validators=[
                            RegexValidator(
                                message="buzz_pubkey must be a lowercase 64-character hex public key",
                                regex="^[0-9a-f]{64}$",
                            )
                        ],
                    ),
                ),
                ("display_name", models.CharField(max_length=255)),
                (
                    "verification_method",
                    models.CharField(
                        choices=[
                            ("operator_attested", "Operator attested"),
                            ("account_challenge", "Account and key challenge"),
                        ],
                        max_length=32,
                    ),
                ),
                ("verification_reference", models.CharField(max_length=255)),
                ("verified_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("revocation_reason", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "community_bridge_identity_link",
                "ordering": ["slack_workspace_id", "slack_user_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="communitybridgeidentitylink",
            constraint=models.UniqueConstraint(
                fields=("slack_workspace_id", "slack_user_id"),
                name="bridge_identity_workspace_slack_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="communitybridgeidentitylink",
            constraint=models.UniqueConstraint(
                fields=("slack_workspace_id", "buzz_pubkey"),
                name="bridge_identity_workspace_buzz_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="communitybridgeidentitylink",
            index=models.Index(
                fields=["slack_workspace_id", "revoked_at"],
                name="bridge_identity_active_idx",
            ),
        ),
    ]
