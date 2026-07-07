from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import integrations.fields

# Sentinel for "no expiry" so newest-token-wins ranking can compare against
# timezone-aware expiries without special-casing None.
_MIN_DT = datetime.min.replace(tzinfo=dt_timezone.utc)


def _resolve_user_id(actor_id, users_by_slack, user_ids):
    """Map a connected_slack_user_id / slack_user_id to a core.User id.

    Actor ids are either a real Slack id or the synthetic ``mlai_user:{id}``
    (see founder_tools.services.actor_ids_for_user).
    """
    value = str(actor_id or "").strip()
    if not value:
        return None
    if value.startswith("mlai_user:"):
        try:
            uid = int(value.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        return uid if uid in user_ids else None
    return users_by_slack.get(value)


def backfill_github_installations(apps, schema_editor):
    """Seed the per-user registry from existing per-org configs + staging rows.

    GitHub access used to live per-company on OrganizationContentConfig (and, for
    legacy Slack flows, on UserIntegration). Fold every row that carries an
    installation into one (user, installation) registry row so a founder's
    installations are shared across all their companies. Newest usable token wins
    on collision. Rows whose owning user can't be resolved are skipped — their
    per-org token still works as a fallback.
    """
    GitHubInstallation = apps.get_model("integrations", "GitHubInstallation")
    UserIntegration = apps.get_model("integrations", "UserIntegration")
    OrganizationContentConfig = apps.get_model("content_factory", "OrganizationContentConfig")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    user_ids = set(User.objects.values_list("id", flat=True))
    users_by_slack = {
        str(slack_id).strip(): uid
        for uid, slack_id in (
            User.objects.exclude(slack_id__isnull=True)
            .exclude(slack_id="")
            .values_list("id", "slack_id")
        )
    }

    candidates = {}

    def _rank(rec):
        return (1 if rec.get("token") else 0, rec.get("expires_at") or _MIN_DT)

    def _consider(user_id, installation_id, *, token, refresh, expires_at, login, scopes):
        installation_id = str(installation_id or "").strip()
        if not user_id or not installation_id:
            return
        incoming = {
            "token": (token or None),
            "refresh": (refresh or None),
            "expires_at": expires_at,
            "login": str(login or "").strip(),
            "scopes": scopes or [],
        }
        key = (user_id, installation_id)
        existing = candidates.get(key)
        if existing is None or _rank(incoming) > _rank(existing):
            candidates[key] = incoming

    for cfg in (
        OrganizationContentConfig.objects.exclude(github_installation_id__isnull=True)
        .exclude(github_installation_id="")
    ):
        _consider(
            _resolve_user_id(cfg.connected_slack_user_id, users_by_slack, user_ids),
            cfg.github_installation_id,
            token=cfg.github_token_encrypted,
            refresh=cfg.github_refresh_token_encrypted,
            expires_at=cfg.github_token_expires_at,
            login=cfg.github_user_name,
            scopes=cfg.github_scopes,
        )

    for integ in (
        UserIntegration.objects.exclude(github_installation_id__isnull=True)
        .exclude(github_installation_id="")
    ):
        _consider(
            _resolve_user_id(integ.slack_user_id, users_by_slack, user_ids),
            integ.github_installation_id,
            token=integ.github_access_token,
            refresh=integ.github_refresh_token,
            expires_at=integ.github_token_expires_at,
            login=integ.github_user_name,
            scopes=integ.github_scopes,
        )

    for (user_id, installation_id), rec in candidates.items():
        GitHubInstallation.objects.update_or_create(
            user_id=user_id,
            installation_id=installation_id,
            defaults={
                "github_user_token_encrypted": rec["token"],
                "github_refresh_token_encrypted": rec["refresh"],
                "github_token_expires_at": rec["expires_at"],
                "github_user_name": rec["login"],
                "account_login": rec["login"],
                "github_scopes": rec["scopes"],
            },
        )


def noop_reverse(apps, schema_editor):
    # Registry rows are re-derivable from per-org configs / staging rows; leave
    # them in place on reverse rather than dropping a founder's shared access.
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("integrations", "0021_googleconnection_organization_and_more"),
        ("content_factory", "0025_orgconfig_use_component_library"),
    ]

    operations = [
        migrations.CreateModel(
            name="GitHubInstallation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("installation_id", models.CharField(db_index=True, max_length=50)),
                ("account_login", models.CharField(blank=True, default="", max_length=255)),
                ("account_type", models.CharField(blank=True, default="", max_length=32)),
                ("github_user_name", models.TextField(blank=True, default="")),
                ("github_user_token_encrypted", integrations.fields.EncryptedTextField(blank=True, null=True)),
                ("github_refresh_token_encrypted", integrations.fields.EncryptedTextField(blank=True, null=True)),
                ("github_token_expires_at", models.DateTimeField(blank=True, null=True)),
                ("github_scopes", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="github_installations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["user_id", "account_login", "installation_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="githubinstallation",
            constraint=models.UniqueConstraint(
                fields=("user", "installation_id"),
                name="uniq_github_installation_user_install",
            ),
        ),
        migrations.RunPython(backfill_github_installations, noop_reverse),
    ]
