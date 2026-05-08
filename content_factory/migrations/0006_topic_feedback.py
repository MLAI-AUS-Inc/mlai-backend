import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('content_factory', '0005_vibe_marketing_component_comment_anchor'),
        ('organizations', '0002_organization_company_linkedin_url'),
    ]

    operations = [
        migrations.CreateModel(
            name='TopicFeedback',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('keyword', models.CharField(max_length=500)),
                ('keyword_normalized', models.CharField(db_index=True, max_length=500)),
                ('feedback_type', models.CharField(db_index=True, default='declined', max_length=32)),
                ('reason_code', models.CharField(default='not_appropriate', max_length=64)),
                ('reason_text', models.TextField(blank=True, null=True)),
                ('decline_scope', models.CharField(default='similar', max_length=32)),
                ('source', models.CharField(default='homepage_topic_card', max_length=80)),
                ('session_id', models.CharField(blank=True, db_index=True, max_length=120, null=True)),
                ('restored_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'organization',
                    models.ForeignKey(
                        db_index=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='topic_feedback',
                        to='organizations.organization',
                    ),
                ),
            ],
            options={
                'db_table': 'seo_topic_feedback',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='topicfeedback',
            index=models.Index(fields=['organization', 'feedback_type', 'restored_at'], name='seo_tf_org_type_active_idx'),
        ),
        migrations.AddIndex(
            model_name='topicfeedback',
            index=models.Index(fields=['organization', 'keyword_normalized'], name='seo_tf_org_keyword_idx'),
        ),
    ]
