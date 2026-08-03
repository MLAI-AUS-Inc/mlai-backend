from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_identity_users(apps, schema_editor):
    User = apps.get_model("core", "User")
    Device = apps.get_model("community_chat", "CommunityChatDevice")
    IdentityLink = apps.get_model("integrations", "CommunityBridgeIdentityLink")

    for link in IdentityLink.objects.filter(user__isnull=True).iterator():
        user = User.objects.filter(slack_id=link.slack_user_id).first()
        if user is None:
            continue
        device_user_ids = set(
            Device.objects.filter(
                public_key=link.buzz_pubkey,
                status="verified",
                revoked_at__isnull=True,
            ).values_list("user_id", flat=True)
        )
        if device_user_ids and user.pk not in device_user_ids:
            # Leave ambiguous/mismatched historical rows for manual reconciliation.
            continue
        IdentityLink.objects.filter(pk=link.pk).update(user_id=user.pk)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("community_chat", "0004_bootstrap_token_origin"),
        ("integrations", "0037_community_bridge_reaction_delivery_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="communitybridgeidentitylink",
            name="user",
            field=models.ForeignKey(
                blank=True,
                help_text="Authoritative MLAI account. Null is supported only for legacy links pending reconciliation.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="community_bridge_identity_links",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_identity_users, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="communitybridgeidentitylink",
            constraint=models.UniqueConstraint(
                condition=models.Q(("user__isnull", False)),
                fields=("slack_workspace_id", "user"),
                name="bridge_identity_workspace_user_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="communitybridgeidentitylink",
            index=models.Index(
                fields=["user", "revoked_at"],
                name="bridge_identity_user_idx",
            ),
        ),
    ]
