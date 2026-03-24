from django.db import migrations, models


def backfill_connected_slack_user_id(apps, schema_editor):
    OrganizationContentConfig = apps.get_model("core", "OrganizationContentConfig")
    UserIntegration = apps.get_model("integrations", "UserIntegration")

    repo_to_user = {}
    repo_to_scan_state = {}
    for integration in UserIntegration.objects.exclude(github_repo__isnull=True).exclude(github_repo=""):
        slack_user_id = getattr(integration, "slack_user_id", None)
        github_repo = getattr(integration, "github_repo", None)
        if not slack_user_id or not github_repo:
            continue

        if github_repo in repo_to_user and repo_to_user[github_repo] != slack_user_id:
            repo_to_user[github_repo] = None
        elif github_repo not in repo_to_user:
            repo_to_user[github_repo] = slack_user_id

        repo_to_scan_state[github_repo] = {
            "last_scanned_sha": getattr(integration, "last_scanned_sha", None),
            "last_scanned_at": getattr(integration, "last_scanned_at", None),
        }

    github_user_to_slack = {}
    for integration in UserIntegration.objects.exclude(github_user_name__isnull=True).exclude(github_user_name=""):
        slack_user_id = getattr(integration, "slack_user_id", None)
        github_user_name = getattr(integration, "github_user_name", None)
        if not slack_user_id or not github_user_name:
            continue

        if github_user_name in github_user_to_slack and github_user_to_slack[github_user_name] != slack_user_id:
            github_user_to_slack[github_user_name] = None
        elif github_user_name not in github_user_to_slack:
            github_user_to_slack[github_user_name] = slack_user_id

    for config in OrganizationContentConfig.objects.all():
        if getattr(config, "connected_slack_user_id", None):
            continue

        owner = None
        github_repo = getattr(config, "github_repo", None)
        github_user_name = getattr(config, "github_user_name", None)

        if github_repo:
            owner = repo_to_user.get(github_repo)
        if not owner and github_user_name:
            owner = github_user_to_slack.get(github_user_name)

        if owner:
            config.connected_slack_user_id = owner
        scan_state = repo_to_scan_state.get(github_repo or "")
        update_fields = []
        if owner:
            update_fields.append("connected_slack_user_id")
        if scan_state and not getattr(config, "last_scanned_sha", None) and scan_state.get("last_scanned_sha"):
            config.last_scanned_sha = scan_state["last_scanned_sha"]
            update_fields.append("last_scanned_sha")
        if scan_state and not getattr(config, "last_scanned_at", None) and scan_state.get("last_scanned_at"):
            config.last_scanned_at = scan_state["last_scanned_at"]
            update_fields.append("last_scanned_at")
        if update_fields:
            config.save(update_fields=update_fields)


def clear_connected_slack_user_id(apps, schema_editor):
    OrganizationContentConfig = apps.get_model("core", "OrganizationContentConfig")
    OrganizationContentConfig.objects.update(
        connected_slack_user_id=None,
        last_scanned_sha=None,
        last_scanned_at=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0005_add_github_refresh_token_fields"),
        ("core", "0040_alter_contentfactoryjob_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="connected_slack_user_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Slack user ID that owns this domain-to-GitHub connection",
                max_length=50,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="last_scanned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="last_scanned_sha",
            field=models.CharField(blank=True, max_length=40, null=True),
        ),
        migrations.RunPython(
            backfill_connected_slack_user_id,
            reverse_code=clear_connected_slack_user_id,
        ),
    ]
