from django.db import migrations, models


def split_company_context(context, fallback_target_audience=""):
    raw = str(context or "").strip()
    fallback = str(fallback_target_audience or "").strip()
    if not raw:
        return "", "", fallback

    marker_problem = "Problem solved:"
    marker_audience = "Target audience:"
    lower = raw.lower()
    problem_index = lower.find(marker_problem.lower())
    audience_index = lower.find(marker_audience.lower())

    short_end = len(raw)
    if problem_index >= 0:
        short_end = min(short_end, problem_index)
    if audience_index >= 0:
        short_end = min(short_end, audience_index)
    short_description = raw[:short_end].strip() or raw

    problem_solved = ""
    if problem_index >= 0:
        problem_start = problem_index + len(marker_problem)
        problem_end = audience_index if audience_index > problem_start else len(raw)
        problem_solved = raw[problem_start:problem_end].strip()

    target_audience = fallback
    if audience_index >= 0:
        audience_start = audience_index + len(marker_audience)
        target_audience = raw[audience_start:].strip() or fallback

    return short_description, problem_solved, target_audience


def backfill_company_detail_fields(apps, schema_editor):
    StartupProfile = apps.get_model("startup_updates", "StartupProfile")
    OrganizationContentConfig = apps.get_model("content_factory", "OrganizationContentConfig")

    for profile in StartupProfile.objects.all().only("id", "organization_id", "notes"):
        config = (
            OrganizationContentConfig.objects.filter(organization_id=profile.organization_id)
            .only("company_context")
            .first()
        )
        short_description, problem_solved, target_audience = split_company_context(
            getattr(config, "company_context", "") if config else "",
            profile.notes,
        )
        update_fields = []
        if short_description:
            profile.short_description = short_description
            update_fields.append("short_description")
        if problem_solved:
            profile.problem_solved = problem_solved
            update_fields.append("problem_solved")
        if target_audience:
            profile.target_audience = target_audience
            update_fields.append("target_audience")
        if update_fields:
            profile.save(update_fields=update_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0012_research_automations"),
        ("startup_updates", "0010_startup_manual_document"),
    ]

    operations = [
        migrations.AddField(
            model_name="startupprofile",
            name="short_description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="startupprofile",
            name="problem_solved",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="startupprofile",
            name="target_audience",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(backfill_company_detail_fields, migrations.RunPython.noop),
    ]
