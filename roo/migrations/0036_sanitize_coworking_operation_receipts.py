from copy import deepcopy

from django.conf import settings
from django.db import migrations


def sanitize_receipts(apps, schema_editor):
    Operation = apps.get_model('roo', 'CoworkingBookingOperation')
    app_label, model_name = settings.AUTH_USER_MODEL.split('.', 1)
    User = apps.get_model(app_label, model_name)

    for operation in Operation.objects.all().iterator(chunk_size=500):
        payload = deepcopy(operation.response_payload)
        if not isinstance(payload, dict):
            continue
        subject_ids = set()
        if operation.kind == 'single':
            user_id = payload.pop('user', None)
            payload.pop('user_email', None)
            if user_id:
                subject_ids.add(user_id)
        else:
            rows = payload.get('results')
            rows = rows if isinstance(rows, list) else []
            rows = sorted(
                rows,
                key=lambda row: (
                    str(row.get('slack_user_id') or '')
                    if isinstance(row, dict)
                    else ''
                ),
            )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row.pop('slack_user_id', None)
                booking = row.get('booking')
                if isinstance(booking, dict):
                    user_id = booking.pop('user', None)
                    booking.pop('user_email', None)
                    if user_id:
                        subject_ids.add(user_id)
            payload['results'] = rows
            payload.pop('admin_slack_user_id', None)

        operation.response_payload = payload
        operation.save(update_fields=['response_payload'])
        if subject_ids:
            operation.subjects.add(
                *User.objects.filter(pk__in=subject_ids).values_list('pk', flat=True)
            )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('roo', '0035_coworkingbookingoperation_subjects'),
    ]

    operations = [
        migrations.RunPython(sanitize_receipts, migrations.RunPython.noop),
    ]
