from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('roo', '0027_alter_ledger_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='pointspurchase',
            name='checkout_request_id',
            field=models.CharField(blank=True, db_index=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='pointspurchase',
            name='stripe_checkout_session_url',
            field=models.URLField(blank=True, max_length=2048, null=True),
        ),
        migrations.AddConstraint(
            model_name='pointspurchase',
            constraint=models.UniqueConstraint(
                condition=models.Q(('checkout_request_id__isnull', False)),
                fields=('checkout_request_id', 'pack_id'),
                name='roo_purchase_request_pack_uniq',
            ),
        ),
    ]
