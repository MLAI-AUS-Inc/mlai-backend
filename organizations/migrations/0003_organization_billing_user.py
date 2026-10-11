"""Explicitly requested company billing field; application requires approval."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("organizations", "0002_organization_company_linkedin_url"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.AddField(model_name="organization", name="billing_user",
        field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
            related_name="content_billing_organizations", to=settings.AUTH_USER_MODEL,
            help_text="Founder who explicitly opted in to pay for company content."))]
