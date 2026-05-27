# Generated for the generic hackathon participant app.

from datetime import date

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_watt_the_hack(apps, schema_editor):
    Hackathon = apps.get_model('core', 'Hackathon')
    GenericHackathonAnnouncement = apps.get_model('generic_hackathons', 'GenericHackathonAnnouncement')
    GenericHackathonResource = apps.get_model('generic_hackathons', 'GenericHackathonResource')

    hackathon, _ = Hackathon.objects.update_or_create(
        slug='watt-the-hack',
        defaults={
            'name': 'Watt The Hack',
            'description': 'Build practical AI and software projects for a cleaner, smarter energy future.',
            'start_date': date(2026, 6, 1),
            'end_date': date(2026, 12, 31),
        },
    )

    GenericHackathonAnnouncement.objects.get_or_create(
        hackathon=hackathon,
        title='Welcome to Watt The Hack',
        defaults={
            'body': 'Create or join a team, complete your profile, and submit your project when your prototype is ready.',
        },
    )

    resources = [
        {
            'title': 'Submission Guide',
            'summary': 'What to include when submitting your project.',
            'body': 'Submit a clear project title, a short summary, and links to your demo, repository, or slides. Attach one supporting file if useful.',
            'category': 'Getting Started',
            'order': 10,
        },
        {
            'title': 'Team Formation',
            'summary': 'Create a new team or join an existing team with its team code.',
            'body': 'Every participant should belong to one Watt The Hack team before submitting a project.',
            'category': 'Getting Started',
            'order': 20,
        },
        {
            'title': 'Judging Criteria',
            'summary': 'Projects should be useful, technically credible, and easy to understand.',
            'body': 'Judges will look for problem clarity, quality of execution, practical impact, and a convincing demonstration.',
            'category': 'Judging',
            'order': 30,
        },
    ]
    for resource in resources:
        GenericHackathonResource.objects.get_or_create(
            hackathon=hackathon,
            title=resource['title'],
            defaults=resource,
        )


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('core', '0052_remove_user_role_has_team'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='GenericHackathonResource',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=180)),
                ('summary', models.TextField()),
                ('body', models.TextField(blank=True)),
                ('url', models.URLField(blank=True, null=True)),
                ('category', models.CharField(blank=True, max_length=80)),
                ('order', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('hackathon', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='generic_resources', to='core.hackathon')),
            ],
            options={
                'ordering': ['order', 'title'],
            },
        ),
        migrations.CreateModel(
            name='GenericHackathonTeam',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('team_id', models.PositiveIntegerField(blank=True, null=True)),
                ('team_name', models.CharField(max_length=120)),
                ('avatar_url', models.URLField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('hackathon', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='generic_teams', to='core.hackathon')),
                ('members', models.ManyToManyField(blank=True, related_name='generic_hackathon_teams', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['team_id', 'team_name'],
            },
        ),
        migrations.CreateModel(
            name='GenericHackathonSubmission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=180)),
                ('summary', models.TextField()),
                ('repository_url', models.URLField(blank=True, null=True)),
                ('demo_url', models.URLField(blank=True, null=True)),
                ('slides_url', models.URLField(blank=True, null=True)),
                ('attachment_url', models.URLField(blank=True, null=True)),
                ('attachment_name', models.CharField(blank=True, max_length=255)),
                ('attachment_content_type', models.CharField(blank=True, max_length=120)),
                ('attachment_size', models.PositiveIntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('hackathon', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='generic_submissions', to='core.hackathon')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='submissions', to='generic_hackathons.generichackathonteam')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='generic_hackathon_submissions', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='GenericHackathonAnnouncement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255)),
                ('body', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('author', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='generic_hackathon_announcements', to=settings.AUTH_USER_MODEL)),
                ('hackathon', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='generic_announcements', to='core.hackathon')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='generichackathonteam',
            constraint=models.UniqueConstraint(fields=('hackathon', 'team_id'), name='unique_generic_hackathon_team_id'),
        ),
        migrations.AddConstraint(
            model_name='generichackathonteam',
            constraint=models.UniqueConstraint(fields=('hackathon', 'team_name'), name='unique_generic_hackathon_team_name'),
        ),
        migrations.RunPython(seed_watt_the_hack, migrations.RunPython.noop),
    ]
