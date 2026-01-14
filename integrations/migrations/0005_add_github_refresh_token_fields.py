# Generated manually for GitHub token refresh functionality

from django.db import migrations, models
import integrations.fields


class Migration(migrations.Migration):

    dependencies = [
        ('integrations', '0004_userintegration_last_scanned_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='userintegration',
            name='github_refresh_token',
            field=integrations.fields.EncryptedTextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userintegration',
            name='github_token_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userintegration',
            name='github_installation_id',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
