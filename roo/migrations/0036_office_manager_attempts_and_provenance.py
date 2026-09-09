# Explicit append-only successor for Office Manager operation identity,
# accounting provenance, and retraction fencing.  Do not fold these changes
# into the already-shared 0034/0035 migration identities.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('roo', '0035_protect_office_manager_assignment_day'),
    ]

    operations = [
        migrations.AddField(
            model_name='officemanagerday',
            name='announcement_next_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='coworkingbooking',
            name='purchased_points_cost_microroo',
            field=models.PositiveBigIntegerField(
                blank=True,
                help_text=(
                    'Exact purchased-points allocation consumed by the '
                    'authoritative booking debit; null means historical '
                    'provenance is unavailable.'
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='end_of_day_reminder_attempt_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='end_of_day_reminder_next_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='purchased_points_refunded_microroo',
            field=models.PositiveBigIntegerField(
                default=0,
                help_text=(
                    'Exact purchased-points portion restored by the Office '
                    'Manager refund and removed again if the booking is '
                    'relinquished.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='winner_channel_announcement_attempt_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='winner_channel_announcement_next_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='winner_channel_retraction_attempt_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='winner_channel_retraction_lease_token',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='winner_channel_retraction_next_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='winner_channel_retraction_status',
            field=models.CharField(
                choices=[
                    ('not_required', 'Not required'),
                    ('pending', 'Pending'),
                    ('sending', 'Sending'),
                    ('sent', 'Sent'),
                    ('failed', 'Failed'),
                    ('exhausted', 'Exhausted'),
                ],
                default='not_required',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='winner_dm_attempt_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='officemanagerassignment',
            name='winner_dm_next_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='OfficeManagerClaimAttempt',
            fields=[
                (
                    'attempt_id',
                    models.UUIDField(editable=False, primary_key=True, serialize=False),
                ),
                ('slack_user_id', models.CharField(max_length=50)),
                ('booking_date', models.DateField()),
                (
                    'outcome',
                    models.CharField(
                        choices=[
                            ('claimed', 'Claimed'),
                            (
                                'already_claimed_by_you',
                                'Already claimed by requester',
                            ),
                            (
                                'already_claimed',
                                'Already claimed by another member',
                            ),
                            ('claim_closed', 'Claim closed'),
                            (
                                'office_manager_day_not_found',
                                'Office Manager day not found',
                            ),
                            ('member_not_eligible', 'Member not eligible'),
                            ('refund_unavailable', 'Refund unavailable'),
                            (
                                'attempt_superseded',
                                'Attempt superseded by cancellation',
                            ),
                        ],
                        max_length=50,
                    ),
                ),
                ('message', models.TextField(blank=True, default='')),
                (
                    'assignee_slack_user_id',
                    models.CharField(blank=True, default='', max_length=50),
                ),
                ('existing_booking_converted', models.BooleanField(default=False)),
                ('points_refunded', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('superseded_at', models.DateTimeField(blank=True, null=True)),
                (
                    'assignment',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='claim_attempts',
                        to='roo.officemanagerassignment',
                    ),
                ),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(
                        fields=['slack_user_id', 'booking_date'],
                        name='roo_om_attempt_actor_date',
                    ),
                ],
            },
        ),
    ]
