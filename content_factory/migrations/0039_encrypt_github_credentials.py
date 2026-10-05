from django.db import migrations

from integrations.fields import LegacyPlaintextEncryptedTextField


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0038_delete_seo_topicmap_researchsession"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organizationcontentconfig",
            name="github_token_encrypted",
            field=LegacyPlaintextEncryptedTextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="organizationcontentconfig",
            name="github_refresh_token_encrypted",
            field=LegacyPlaintextEncryptedTextField(blank=True, null=True),
        ),
    ]
