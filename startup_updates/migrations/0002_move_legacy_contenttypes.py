from django.db import migrations


MODEL_NAMES = [
    "startupprofile",
    "userstartupbinding",
    "gmailsynccursor",
    "gmailmessageartifact",
    "gmailthreadartifact",
    "gmailattachmentartifact",
    "slackchannelselection",
    "slackmessageartifact",
    "slackthreadartifact",
    "startupmetricobservation",
    "startupevent",
    "monthlyupdatedraft",
]


def move_legacy_contenttypes(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    LogEntry = apps.get_model("admin", "LogEntry")

    for model_name in MODEL_NAMES:
        source = ContentType.objects.filter(app_label="integrations", model=model_name).first()
        if not source:
            continue

        target = ContentType.objects.filter(app_label="startup_updates", model=model_name).first()
        if target and target.pk != source.pk:
            Permission.objects.filter(content_type=source).update(content_type=target)
            LogEntry.objects.filter(content_type=source).update(content_type=target)
            source.delete()
            continue

        source.app_label = "startup_updates"
        source.save(update_fields=["app_label"])


class Migration(migrations.Migration):

    dependencies = [
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("startup_updates", "0001_split_startup_updates_app"),
        ("integrations", "0016_split_startup_updates_app"),
    ]

    operations = [
        migrations.RunPython(move_legacy_contenttypes, migrations.RunPython.noop),
    ]
