import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [
        ('roo', '0045_cybertest_failed_settlement'),
        ('founder_tools', '0011_company_avatar_url_length'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name='CybertestCompanyBinding',
        fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('workspace_id', models.UUIDField(unique=True)),
            ('is_active', models.BooleanField(default=False)),
            ('verified_at', models.DateTimeField(null=True, blank=True)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('company', models.OneToOneField(to='founder_tools.viberaisingcompany', on_delete=django.db.models.deletion.PROTECT, related_name='cybertest_binding')),
            ('created_by', models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT, related_name='cybertest_company_bindings')),
        ],
        options={'constraints': [models.CheckConstraint(
            condition=models.Q(is_active=False) | models.Q(verified_at__isnull=False),
            name='cybertest_active_verified')]},
    )]
