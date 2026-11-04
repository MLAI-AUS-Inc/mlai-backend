"""Plan B9/Phase 4: choices only; preserve legacy sites and publication history."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('content_factory', '0049_writtenarticle_run')]
    operations = [migrations.AlterField(model_name='hostedarticlesite', name='state',
        field=models.CharField(max_length=20, default='draft', choices=[('draft', 'Being set up'),
            ('active', 'Publishing'), ('paused', 'Paused'), ('retired', 'Retired')]))]
