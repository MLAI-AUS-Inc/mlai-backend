"""Proposed article outbox; never apply without specific approval."""
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("content_factory", "0044_website_target_generation_key"), ("organizations", "0003_organization_billing_user")]
    operations = [migrations.CreateModel(name="ContentFactoryDispatchOutbox", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("client_request_id", models.CharField(max_length=255, unique=True)),
        ("payload", models.JSONField(default=dict)),
        ("attempts", models.PositiveIntegerField(default=0)),
        ("next_attempt_at", models.DateTimeField(default=django.utils.timezone.now)),
        ("state", models.CharField(default="pending", max_length=20)),
        ("run_id", models.CharField(blank=True, default="", max_length=100)),
        ("last_error", models.CharField(blank=True, default="", max_length=100)),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("updated_at", models.DateTimeField(auto_now=True)),
        ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
            related_name="content_dispatch_outbox", to="organizations.organization")),
    ], options={"db_table": "content_factory_dispatch_outbox",
        "indexes": [models.Index(fields=["state", "next_attempt_at"], name="cf_dispatch_outbox_due_idx")]})]
