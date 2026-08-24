# Generated for MLAI Coding Roo billing.

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
from django.db.models import F
import django.db.models.deletion
import django.utils.timezone
import roo.models


SCALE = 1_000_000
PRICING_VERSION = "do-kimi-k3-2026-08"


def forwards_precision_and_pricing(apps, schema_editor):
    PointsAccount = apps.get_model("roo", "PointsAccount")
    Ledger = apps.get_model("roo", "Ledger")
    CodingPricingVersion = apps.get_model("roo", "CodingPricingVersion")

    PointsAccount.objects.update(
        balance_microroo=F("balance") * SCALE,
        earned_balance_microroo=F("earned_balance") * SCALE,
        purchased_topup_balance_microroo=F("purchased_topup_balance") * SCALE,
        lifetime_earned_microroo=F("lifetime_earned") * SCALE,
        lifetime_purchased_topup_microroo=F("lifetime_purchased_topup") * SCALE,
        lifetime_spent_microroo=F("lifetime_spent") * SCALE,
        expired_or_reversed_microroo=F("expired_or_reversed_points") * SCALE,
        microroo_initialized=True,
    )
    Ledger.objects.filter(delta__isnull=False).update(
        delta_microroo=F("delta") * SCALE,
    )
    Ledger.objects.filter(delta__isnull=True, points_delta__isnull=False).update(
        # Older rows may only populate the deprecated points_delta column. Keep
        # the new canonical exact amount complete for those rows as well.
        delta_microroo=F("points_delta") * SCALE,
    )
    Ledger.objects.filter(points_delta__isnull=False).update(
        points_delta_microroo=F("points_delta") * SCALE,
    )
    CodingPricingVersion.objects.get_or_create(
        version=PRICING_VERSION,
        defaults={
            "model": "kimi-k3",
            "input_usd_per_million": Decimal("3.000000"),
            "cached_input_usd_per_million": Decimal("0.600000"),
            "output_usd_per_million": Decimal("15.000000"),
            "usd_aud_rate": Decimal("1.500000"),
            "margin_multiplier": Decimal("1.300000"),
            "aud_per_roo": Decimal("1.000000"),
            "is_active": True,
        },
    )


def reverse_pricing(apps, schema_editor):
    CodingPricingVersion = apps.get_model("roo", "CodingPricingVersion")
    CodingPricingVersion.objects.filter(version=PRICING_VERSION).delete()


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("community_chat", "0007_active_installation_constraint"),
        ("roo", "0029_boostpostadmission_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="pointsaccount",
            name="balance_microroo",
            field=models.BigIntegerField(default=0, help_text="Current spendable balance in microroo"),
        ),
        migrations.AddField(
            model_name="pointsaccount",
            name="earned_balance_microroo",
            field=models.BigIntegerField(default=0, help_text="Earned balance in microroo"),
        ),
        migrations.AddField(
            model_name="pointsaccount",
            name="purchased_topup_balance_microroo",
            field=models.BigIntegerField(default=0, help_text="Purchased balance in microroo"),
        ),
        migrations.AddField(
            model_name="pointsaccount",
            name="lifetime_earned_microroo",
            field=models.BigIntegerField(default=0, help_text="Lifetime earned in microroo"),
        ),
        migrations.AddField(
            model_name="pointsaccount",
            name="lifetime_purchased_topup_microroo",
            field=models.BigIntegerField(default=0, help_text="Lifetime purchased in microroo"),
        ),
        migrations.AddField(
            model_name="pointsaccount",
            name="lifetime_spent_microroo",
            field=models.BigIntegerField(default=0, help_text="Lifetime spent in microroo"),
        ),
        migrations.AddField(
            model_name="pointsaccount",
            name="expired_or_reversed_microroo",
            field=models.BigIntegerField(default=0, help_text="Expired or reversed amount in microroo"),
        ),
        migrations.AddField(
            model_name="pointsaccount",
            name="microroo_initialized",
            field=models.BooleanField(default=False, help_text="Whether precision fields have been initialized from legacy whole Roo"),
        ),
        migrations.AddField(
            model_name="ledger",
            name="delta_microroo",
            field=models.BigIntegerField(blank=True, help_text="Exact change in microroo", null=True),
        ),
        migrations.AddField(
            model_name="ledger",
            name="points_delta_microroo",
            field=models.BigIntegerField(blank=True, help_text="DEPRECATED exact legacy change in microroo", null=True),
        ),
        migrations.CreateModel(
            name="CodingPricingVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version", models.CharField(max_length=80, unique=True)),
                ("model", models.CharField(default="kimi-k3", max_length=80)),
                ("input_usd_per_million", models.DecimalField(decimal_places=6, max_digits=12)),
                ("cached_input_usd_per_million", models.DecimalField(decimal_places=6, max_digits=12)),
                ("output_usd_per_million", models.DecimalField(decimal_places=6, max_digits=12)),
                ("usd_aud_rate", models.DecimalField(decimal_places=6, max_digits=12)),
                ("margin_multiplier", models.DecimalField(decimal_places=6, default="1.300000", max_digits=8)),
                ("aud_per_roo", models.DecimalField(decimal_places=6, default="1.000000", max_digits=12)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("effective_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ("-effective_at", "-created_at")},
        ),
        migrations.CreateModel(
            name="CodingTurn",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("device_id", models.UUIDField()),
                ("local_session_id", models.UUIDField()),
                ("idempotency_key", models.UUIDField()),
                ("model", models.CharField(default="kimi-k3", max_length=80)),
                ("status", models.CharField(choices=[("active", "Active"), ("reconciling", "Reconciling"), ("completed", "Completed"), ("cancelled", "Cancelled"), ("failed", "Failed")], db_index=True, default="active", max_length=20)),
                ("reserved_microroo", models.BigIntegerField(default=0)),
                ("settled_microroo", models.BigIntegerField(default=0)),
                ("released_microroo", models.BigIntegerField(default=0)),
                ("finalize_outcome", models.CharField(blank=True, max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(db_index=True, default=roo.models.default_coding_turn_expires_at)),
                ("account_session", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="coding_turns", to="community_chat.communitychataccountsession")),
                ("pricing_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="turns", to="roo.codingpricingversion")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="coding_turns", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(fields=["user", "status"], name="roo_coding_turn_usr_status_idx"),
                    models.Index(fields=["device_id", "status"], name="roo_coding_turn_device_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("user", "idempotency_key"), name="roo_coding_turn_user_idem_uniq"),
                    models.UniqueConstraint(condition=models.Q(status__in=("active", "reconciling")), fields=("user",), name="roo_coding_one_open_turn_user"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CodingModelCall",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("call_id", models.UUIDField()),
                ("status", models.CharField(choices=[("reserved", "Reserved"), ("settled", "Settled"), ("released", "Released"), ("ambiguous", "Ambiguous")], db_index=True, default="reserved", max_length=20)),
                ("estimated_input_tokens", models.PositiveBigIntegerField(default=0)),
                ("requested_output_tokens", models.PositiveBigIntegerField(default=0)),
                ("max_output_tokens", models.PositiveBigIntegerField(default=0)),
                ("reserved_microroo", models.BigIntegerField(default=0)),
                ("charged_microroo", models.BigIntegerField(default=0)),
                ("calculated_microroo", models.BigIntegerField(default=0)),
                ("pricing_version_snapshot", models.CharField(default="do-kimi-k3-2026-08", max_length=80)),
                ("input_usd_per_million", models.DecimalField(decimal_places=6, default="3.000000", max_digits=12)),
                ("cached_input_usd_per_million", models.DecimalField(decimal_places=6, default="0.600000", max_digits=12)),
                ("output_usd_per_million", models.DecimalField(decimal_places=6, default="15.000000", max_digits=12)),
                ("usd_aud_rate", models.DecimalField(decimal_places=6, default="1.500000", max_digits=12)),
                ("margin_multiplier", models.DecimalField(decimal_places=6, default="1.300000", max_digits=8)),
                ("aud_per_roo", models.DecimalField(decimal_places=6, default="1.000000", max_digits=12)),
                ("input_tokens", models.PositiveBigIntegerField(default=0)),
                ("cached_input_tokens", models.PositiveBigIntegerField(default=0)),
                ("output_tokens", models.PositiveBigIntegerField(default=0)),
                ("provider_request_id", models.CharField(blank=True, max_length=255)),
                ("trace_id", models.CharField(blank=True, max_length=255)),
                ("dispatch_owner_hash", models.CharField(max_length=64)),
                ("dispatch_lease_expires_at", models.DateTimeField(db_index=True)),
                ("dispatch_started_at", models.DateTimeField(blank=True, null=True)),
                ("failure_reason", models.CharField(blank=True, max_length=500)),
                ("reconcile_after", models.DateTimeField(blank=True, null=True)),
                ("reserved_at", models.DateTimeField(auto_now_add=True)),
                ("settled_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("ledger_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="coding_model_call", to="roo.ledger")),
                ("turn", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="model_calls", to="roo.codingturn")),
            ],
            options={
                "ordering": ("reserved_at",),
                "indexes": [
                    models.Index(fields=["status", "reconcile_after"], name="roo_coding_call_status_age_idx"),
                    models.Index(fields=["status", "dispatch_lease_expires_at"], name="roo_coding_call_dispatch_idx"),
                ],
                "constraints": [models.UniqueConstraint(fields=("turn", "call_id"), name="roo_coding_call_turn_call_uniq")],
            },
        ),
        migrations.RunPython(forwards_precision_and_pricing, reverse_pricing),
    ]
