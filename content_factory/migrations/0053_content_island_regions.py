"""Stable topic regions for the scalable content map; no data backfill."""
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('content_factory', '0052_author_photo'),
        ('organizations', '0001_split_content_factory_apps'),
    ]
    operations = [
        migrations.CreateModel(
            name='ContentIslandRegion',
            fields=[
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('slug', models.SlugField(max_length=80)),
                ('name', models.CharField(max_length=80)),
                ('name_source', models.CharField(max_length=16, default='fallback', choices=[
                    ('fallback', 'Fallback'), ('llm', 'Generated'), ('founder', 'Founder')])),
                ('color_key', models.CharField(max_length=32)),
                ('centroid_embedding', models.JSONField(default=list, blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(to='organizations.organization', db_index=True,
                    on_delete=django.db.models.deletion.CASCADE, related_name='content_island_regions')),
            ],
            options={'db_table': 'seo_content_island_region', 'ordering': ['slug'],
                     'unique_together': {('organization', 'slug')}},
        ),
        migrations.AddField(model_name='contentisland', name='region',
            field=models.ForeignKey(to='content_factory.contentislandregion', null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL, related_name='islands')),
    ]
