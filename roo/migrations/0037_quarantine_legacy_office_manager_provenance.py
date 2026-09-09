"""Keep unknown historical Office Manager point allocations explicit.

0036 introduced exact bucket provenance, but its non-null assignment default
could not distinguish a proven all-earned refund from a legacy refund whose
allocation was never recorded.  This append-only successor quarantines those
legacy refunded assignments until an operator verifies their source ledger.
"""

from django.db import migrations, models
import django.db.models.deletion


def quarantine_legacy_refund_allocations(apps, schema_editor):
    OfficeManagerAssignment = apps.get_model("roo", "OfficeManagerAssignment")
    unknown = OfficeManagerAssignment.objects.filter(
        points_refunded__gt=0,
        refund_ledger_entry_id__isnull=False,
    )
    unknown.update(purchased_points_refunded_microroo=None)

    # 0036-era writes already recorded exact provenance on the booking. Keep
    # those known allocations, but only after proving the linked refund ledger
    # and booking total agree. Older rows remain null for operator review.
    for assignment in unknown.select_related(
        "booking",
        "refund_ledger_entry",
    ).iterator():
        booking = assignment.booking
        refund = assignment.refund_ledger_entry
        total_microroo = int(assignment.points_refunded) * 1_000_000
        purchased_microroo = booking.purchased_points_cost_microroo
        if (
            purchased_microroo is not None
            and 0 <= purchased_microroo <= total_microroo
            and booking.original_points_cost == assignment.points_refunded
            and refund is not None
            and refund.user_id == assignment.user_id
            and refund.kind == "REFUND"
            and refund.source == "COWORKING"
            and refund.delta_microroo == total_microroo
            and refund.reference_type == "OFFICE_MANAGER_ASSIGNMENT"
            and refund.reference_id == str(assignment.day_id)
        ):
            OfficeManagerAssignment.objects.filter(pk=assignment.pk).update(
                purchased_points_refunded_microroo=purchased_microroo
            )


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0036_office_manager_attempts_and_provenance"),
    ]

    operations = [
        migrations.AlterField(
            model_name="officemanagerassignment",
            name="purchased_points_refunded_microroo",
            field=models.PositiveBigIntegerField(
                blank=True,
                help_text=(
                    "Exact purchased-points portion restored by the Office "
                    "Manager refund and removed again if the booking is "
                    "relinquished; null means historical provenance must be "
                    "reconciled."
                ),
                null=True,
            ),
        ),
        migrations.RunPython(
            quarantine_legacy_refund_allocations,
            migrations.RunPython.noop,
        ),
        migrations.CreateModel(
            name="OfficeManagerProvenanceReconciliation",
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
                ("assignment_refund_snapshot", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "booking",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="office_manager_provenance_reconciliation",
                        to="roo.coworkingbooking",
                    ),
                ),
                (
                    "debit_ledger",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="office_manager_provenance_reconciliations",
                        to="roo.ledger",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
    ]
