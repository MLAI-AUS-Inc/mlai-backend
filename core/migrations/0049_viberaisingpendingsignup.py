from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0048_seed_innovate_connect_alliance_hackathon'),
    ]

    operations = [
        migrations.CreateModel(
            name='VibeRaisingPendingSignup',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(db_index=True, max_length=254)),
                ('app', models.CharField(default='vibe-raising', max_length=64)),
                ('next_path', models.CharField(blank=True, max_length=255, null=True)),
                ('role', models.CharField(choices=[('admin', 'Admin'), ('participant', 'Participant'), ('professional', 'Professional')], default='participant', max_length=20)),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddIndex(
            model_name='viberaisingpendingsignup',
            index=models.Index(fields=['app', 'email', 'used_at'], name='core_vibera_app_f19741_idx'),
        ),
    ]
