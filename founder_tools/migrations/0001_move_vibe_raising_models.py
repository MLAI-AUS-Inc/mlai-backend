from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("vibe_raising", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="VibeRaisingCompany",
                    fields=[
                        ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ("name", models.CharField(max_length=255)),
                        ("domain", models.CharField(blank=True, max_length=255, null=True)),
                        ("abn", models.CharField(blank=True, max_length=64, null=True)),
                        ("registered", models.BooleanField(default=False)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        "db_table": "vibe_raising_viberaisingcompany",
                        "ordering": ["created_at", "name"],
                    },
                ),
                migrations.CreateModel(
                    name="VibeRaisingProfile",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        (
                            "role",
                            models.CharField(
                                choices=[("founder", "Founder"), ("investor", "Investor")],
                                max_length=16,
                            ),
                        ),
                        ("organization_name", models.CharField(blank=True, max_length=255, null=True)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        (
                            "active_company",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="+",
                                to="founder_tools.viberaisingcompany",
                            ),
                        ),
                        (
                            "user",
                            models.OneToOneField(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="vibe_raising_profile",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "db_table": "vibe_raising_viberaisingprofile",
                        "ordering": ["user_id"],
                    },
                ),
                migrations.AddField(
                    model_name="viberaisingcompany",
                    name="profile",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="companies",
                        to="founder_tools.viberaisingprofile",
                    ),
                ),
            ],
        ),
    ]
