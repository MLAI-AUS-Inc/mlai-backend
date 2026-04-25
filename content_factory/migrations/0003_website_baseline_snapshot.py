from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("content_factory", "0002_move_legacy_contenttypes"),
        ("organizations", "0001_split_content_factory_apps"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="baseline_skip_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="baseline_skipped_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the founder explicitly skipped the website baseline prerequisite.",
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="WebsiteBaselineSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(db_index=True, max_length=255)),
                ("run_id", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("completed", "Completed"),
                            ("partial", "Partial"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        db_index=True,
                        default="completed",
                        max_length=20,
                    ),
                ),
                ("collected_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("overall_score", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("metrics", models.JSONField(blank=True, default=dict)),
                ("source_status", models.JSONField(blank=True, default=dict)),
                ("recommendations", models.JSONField(blank=True, default=list)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="website_baseline_snapshots",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "db_table": "content_factory_website_baseline_snapshot",
                "ordering": ["-collected_at", "-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="websitebaselinesnapshot",
            index=models.Index(fields=["organization", "-collected_at"], name="website_base_org_collected_idx"),
        ),
        migrations.AddIndex(
            model_name="websitebaselinesnapshot",
            index=models.Index(fields=["domain", "-collected_at"], name="website_base_domain_idx"),
        ),
    ]
