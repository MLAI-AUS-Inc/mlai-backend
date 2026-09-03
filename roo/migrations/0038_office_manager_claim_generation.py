from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


def fence_pre_generation_reopened_days(apps, schema_editor):
    Day = apps.get_model("roo", "OfficeManagerDay")
    Assignment = apps.get_model("roo", "OfficeManagerAssignment")
    Attempt = apps.get_model("roo", "OfficeManagerClaimAttempt")
    reopened_days = Day.objects.filter(
        pk__in=Assignment.objects.filter(
            status="relinquished",
            day__status="open",
        ).values_list("day_id", flat=True)
    )
    reopened_day_ids = reopened_days.values_list("pk", flat=True)
    reopened_dates = reopened_days.values_list("date", flat=True)
    Attempt.objects.filter(
        booking_date__in=reopened_dates,
        generation=1,
    ).exclude(outcome="attempt_superseded").update(
        outcome="attempt_superseded",
        message=(
            "This Office Manager claim attempt was superseded by "
            "cancellation and cannot be replayed"
        ),
        superseded_at=timezone.now(),
    )
    Day.objects.filter(pk__in=reopened_day_ids).update(
        generation=2,
        message_update_pending=True,
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0037_quarantine_legacy_office_manager_provenance"),
    ]

    operations = [
        migrations.AddField(
            model_name="officemanagerday",
            name="generation",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="officemanagerclaimattempt",
            name="generation",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(
            fence_pre_generation_reopened_days,
            noop_reverse,
        ),
        migrations.AddField(
            model_name="officemanagerassignment",
            name="winner_dm_message_ts",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.AddField(
            model_name="officemanagerassignment",
            name="end_of_day_reminder_message_ts",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.AddField(
            model_name="officemanagerassignment",
            name="private_correction_pending",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="officemanagerassignment",
            name="private_correction_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("sending", "Sending"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("unknown", "Unknown"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="officemanagerassignment",
            name="private_correction_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="officemanagerassignment",
            name="private_correction_last_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="officemanagerassignment",
            name="private_correction_attempt_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="officemanagerassignment",
            name="private_correction_next_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="officemanagerclaimattempt",
            name="outcome",
            field=models.CharField(
                choices=[
                    ("claimed", "Claimed"),
                    ("already_claimed_by_you", "Already claimed by requester"),
                    ("already_claimed", "Already claimed by another member"),
                    ("claim_closed", "Claim closed"),
                    ("office_manager_day_not_found", "Office Manager day not found"),
                    ("member_not_eligible", "Member not eligible"),
                    ("refund_unavailable", "Refund unavailable"),
                    ("attempt_superseded", "Attempt superseded by cancellation"),
                    ("announcement_superseded", "Announcement generation superseded"),
                ],
                max_length=50,
            ),
        ),
        migrations.CreateModel(
            name="OfficeManagerProvenanceBucketRepair",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("purchased_microroo", models.PositiveBigIntegerField()),
                ("account_before", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "ledger",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="office_manager_bucket_repair",
                        to="roo.ledger",
                    ),
                ),
                (
                    "reconciliation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bucket_repair",
                        to="roo.officemanagerprovenancereconciliation",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.CreateModel(
            name="OfficeManagerRefundReversalProvenance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("purchased_microroo", models.PositiveBigIntegerField()),
                ("reviewed_by", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "assignment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="refund_reversal_provenance",
                        to="roo.officemanagerassignment",
                    ),
                ),
                (
                    "reversal_ledger",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="office_manager_refund_reversal_provenance",
                        to="roo.ledger",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.CreateModel(
            name="ScheduledDiscoveryHeartbeat",
            fields=[
                (
                    "name",
                    models.CharField(
                        max_length=80,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("last_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_succeeded_at", models.DateTimeField(blank=True, null=True)),
                ("last_failed_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
    ]
