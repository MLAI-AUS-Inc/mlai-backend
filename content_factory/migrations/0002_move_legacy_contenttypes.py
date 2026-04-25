from django.db import migrations


MODEL_APP_LABELS = {
    "organization": "organizations",
    "contentfactoryrun": "workflow_runs",
    "contentfactoryrunstep": "workflow_runs",
    "contentfactoryrunstepattempt": "workflow_runs",
    "organizationcontentconfig": "content_factory",
    "generatedcomponent": "content_factory",
    "componentmapping": "content_factory",
    "contentfactoryjob": "content_factory",
    "scheduleddiscoverydispatch": "content_factory",
    "contentfactoryhealingrecord": "content_factory",
    "writtenarticle": "content_factory",
    "researchedkeyword": "content_factory",
    "keywordvelocity": "content_factory",
    "aisaturation": "content_factory",
    "paquestion": "content_factory",
    "semanticcluster": "content_factory",
    "clustermembership": "content_factory",
    "topicmap": "content_factory",
    "researchsession": "content_factory",
}


def move_legacy_contenttypes(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    LogEntry = apps.get_model("admin", "LogEntry")

    for model_name, target_app_label in MODEL_APP_LABELS.items():
        source = ContentType.objects.filter(app_label="core", model=model_name).first()
        if not source:
            continue

        target = ContentType.objects.filter(app_label=target_app_label, model=model_name).first()
        if target and target.pk != source.pk:
            Permission.objects.filter(content_type=source).update(content_type=target)
            LogEntry.objects.filter(content_type=source).update(content_type=target)
            source.delete()
            continue

        source.app_label = target_app_label
        source.save(update_fields=["app_label"])


class Migration(migrations.Migration):

    dependencies = [
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("core", "0051_split_content_factory_apps"),
        ("organizations", "0001_split_content_factory_apps"),
        ("workflow_runs", "0001_split_content_factory_apps"),
        ("content_factory", "0001_split_content_factory_apps"),
    ]

    operations = [
        migrations.RunPython(move_legacy_contenttypes, migrations.RunPython.noop),
    ]
