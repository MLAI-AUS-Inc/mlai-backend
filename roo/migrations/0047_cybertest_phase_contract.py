"""Authorized for creation only on 2026-11-12; execution requires separate approval."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('roo', '0046_cybertest_company_binding')]

    operations = [
        migrations.AddField(
            model_name='cybertestcheckout', name='parent_checkout',
            field=models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='upgrade_checkouts', to='roo.cybertestcheckout')),
        migrations.AddField(
            model_name='cybertestcheckout', name='phase_contract',
            field=models.JSONField(default=dict)),
        migrations.AddField(
            model_name='cybertestcheckout', name='delivery_evidence',
            field=models.JSONField(null=True, blank=True)),
        migrations.AddConstraint(
            model_name='cybertestcheckout',
            constraint=models.UniqueConstraint(
                fields=['parent_checkout'],
                condition=models.Q(parent_checkout__isnull=False, status__in=['quoted', 'authorized', 'charged', 'started']),
                name='cybertest_one_active_upgrade')),
        migrations.AddConstraint(
            model_name='cybertestcheckout',
            constraint=models.CheckConstraint(
                condition=models.Q(parent_checkout__isnull=True) | ~models.Q(parent_checkout=models.F('id')),
                name='cybertest_not_own_parent')),
    ]
