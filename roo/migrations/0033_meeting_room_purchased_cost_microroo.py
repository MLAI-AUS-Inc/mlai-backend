from django.db import migrations, models
from django.db.models import F, Value
from django.db.models.functions import Coalesce


MICROROO_PER_ROO = 1_000_000


def backfill_purchased_cost_microroo(apps, schema_editor):
    MeetingRoomBooking = apps.get_model('roo', 'MeetingRoomBooking')
    MeetingRoomBooking.objects.update(
        purchased_points_cost_microroo=(
            Coalesce(F('purchased_points_cost'), Value(0)) * MICROROO_PER_ROO
        )
    )


class Migration(migrations.Migration):

    dependencies = [
        ('roo', '0032_small_and_big_meeting_rooms'),
    ]

    operations = [
        migrations.AddField(
            model_name='meetingroombooking',
            name='purchased_points_cost_microroo',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.RunPython(
            backfill_purchased_cost_microroo,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='meetingroombooking',
            constraint=models.CheckConstraint(
                check=models.Q(
                    purchased_points_cost_microroo__lte=(
                        models.F('points_cost') * 1_000_000
                    )
                ),
                name='meeting_room_purchased_micro_lte_total',
            ),
        ),
    ]
