from django.db import migrations

# Django's DeleteModel does not remove the model's django_content_type row, so
# the models dropped in the 2026-09 cleanup left content types behind, each
# still carrying four auth_permission rows. core.0058 purged the content types
# for the removed *apps*; this migration finishes the job for the models that
# were deleted from apps that still exist.
STALE_CONTENT_TYPES = (
    # hospital.0017_delete_medhack_game_and_prediction
    ("hospital", "medhackcase"),
    ("hospital", "medhackguess"),
    ("hospital", "medhackwinner"),
    ("hospital", "prediction"),
    # content_factory.0038_delete_seo_topicmap_researchsession
    ("content_factory", "topicmap"),
    ("content_factory", "researchsession"),
    # org_memory.0025_delete_selector_shadow
    ("org_memory", "memoryselectorshadowrun"),
    ("org_memory", "memoryselectorshadowresult"),
)

# org_memory's GenericForeignKeys PROTECT django_content_type. None of them
# reference these rows today, but a silent orphan would be worse than a loud
# failure, so the purge refuses to run if that ever stops being true.
PROTECTED_REFERENCES = (
    ("org_memory", "MemoryReviewItem", "target_content_type"),
    ("org_memory", "MemoryPublication", "source_content_type"),
)


def purge_stale_content_types(apps, schema_editor):
    from django.apps import apps as live_apps

    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    LogEntry = apps.get_model("admin", "LogEntry")

    pairs = []
    for app_label, model in STALE_CONTENT_TYPES:
        # Never delete the content type of a model that exists again: this
        # migration is about leftovers, not about live models.
        try:
            live_apps.get_model(app_label, model)
        except LookupError:
            pairs.append((app_label, model))

    stale = ContentType.objects.none()
    for app_label, model in pairs:
        stale = stale | ContentType.objects.filter(app_label=app_label, model=model)

    stale_ids = list(stale.values_list("id", flat=True))
    if not stale_ids:
        return

    existing_tables = set(schema_editor.connection.introspection.table_names())
    for app_label, model_name, field in PROTECTED_REFERENCES:
        model = apps.get_model(app_label, model_name)
        if model._meta.db_table not in existing_tables:
            continue
        referencing = model.objects.filter(**{f"{field}__in": stale_ids}).count()
        if referencing:
            raise RuntimeError(
                f"{model._meta.db_table}.{field} still references {referencing} "
                "stale content type(s); refusing to orphan a protected "
                "generic reference."
            )

    # Mirror the declared on_delete behaviour of every FK into django_content_type:
    # Permission CASCADEs, LogEntry SET_NULLs. The database constraints are
    # NO ACTION, so both have to be handled before the delete.
    Permission.objects.filter(content_type_id__in=stale_ids).delete()
    LogEntry.objects.filter(content_type_id__in=stale_ids).update(content_type=None)
    ContentType.objects.filter(id__in=stale_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0058_drop_orphan_tables_from_removed_apps"),
        ("admin", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("hospital", "0017_delete_medhack_game_and_prediction"),
        ("content_factory", "0038_delete_seo_topicmap_researchsession"),
        ("org_memory", "0025_delete_selector_shadow"),
    ]

    operations = [
        migrations.RunPython(
            purge_stale_content_types,
            migrations.RunPython.noop,
        ),
    ]
