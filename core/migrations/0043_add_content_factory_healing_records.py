from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0042_add_article_delivery_mode_to_org_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="build_healing_hints",
            field=models.JSONField(blank=True, default=list, help_text="Reusable build/browser healing rules promoted from publish-time verification."),
        ),
        migrations.CreateModel(
            name="ContentFactoryHealingRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(db_index=True, max_length=255)),
                ("github_repo", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("failure_kind", models.CharField(db_index=True, max_length=100)),
                ("failure_family_key", models.CharField(db_index=True, max_length=64)),
                ("exact_signature", models.CharField(blank=True, default="", max_length=64)),
                ("summary", models.TextField(blank=True, default="")),
                ("normalized_failure", models.JSONField(blank=True, default=dict)),
                ("changed_files", models.JSONField(blank=True, default=list)),
                ("patch_manifest", models.JSONField(blank=True, default=dict)),
                ("validation_results", models.JSONField(blank=True, default=dict)),
                ("evidence_artifacts", models.JSONField(blank=True, default=dict)),
                ("snippet_or_rule", models.TextField(blank=True, default="")),
                ("applies_to", models.JSONField(blank=True, default=list)),
                ("promoted_payload", models.JSONField(blank=True, default=dict)),
                ("promotion_state", models.CharField(choices=[("candidate", "Candidate"), ("promoted", "Promoted")], db_index=True, default="candidate", max_length=32)),
                ("latest_run_id", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name="content_factory_healing_records", to="core.organization"),
                ),
            ],
            options={
                "db_table": "content_factory_healing_record",
                "ordering": ["-updated_at"],
                "unique_together": {("domain", "github_repo", "failure_kind", "failure_family_key")},
            },
        ),
    ]
