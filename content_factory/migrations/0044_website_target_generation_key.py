"""Preserve website targets separately for each consent generation.

Creation approved by the user on 2026-10-07. Application is separately gated.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("content_factory", "0043_merge_credentials_website")]

    operations = [
        migrations.AddConstraint(
            model_name="websiteconnectiontarget",
            constraint=models.UniqueConstraint(
                fields=("connection", "generation", "target_key"),
                name="cf_web_target_generation_unique",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="websiteconnectiontarget",
            name="cf_web_target_key_unique",
        ),
    ]
