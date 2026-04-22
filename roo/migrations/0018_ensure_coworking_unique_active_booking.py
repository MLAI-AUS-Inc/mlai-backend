from django.db import migrations


def fail_if_duplicate_active_bookings(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_id, date, COUNT(*) AS duplicate_count
            FROM roo_coworkingbooking
            WHERE status = 'booked'
            GROUP BY user_id, date
            HAVING COUNT(*) > 1
            ORDER BY date, user_id
            LIMIT 10
            """
        )
        duplicates = cursor.fetchall()

    if duplicates:
        formatted = ", ".join(
            f"user_id={user_id} date={booking_date} count={count}"
            for user_id, booking_date, count in duplicates
        )
        raise RuntimeError(
            "Cannot create unique_active_booking_per_user_date because duplicate "
            f"active coworking bookings exist: {formatted}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0017_points_health_indexes"),
    ]

    operations = [
        migrations.RunPython(
            fail_if_duplicate_active_bookings,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RunSQL(
            sql="""
            CREATE UNIQUE INDEX IF NOT EXISTS unique_active_booking_per_user_date
            ON roo_coworkingbooking (user_id, date)
            WHERE status = 'booked';
            """,
            reverse_sql="""
            DROP INDEX IF EXISTS unique_active_booking_per_user_date;
            """,
            state_operations=[],
        ),
    ]
