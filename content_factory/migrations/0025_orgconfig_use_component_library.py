from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('content_factory', '0024_generatedcomponent_import_statement_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationcontentconfig',
            name='use_component_library',
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Opt-in: when true, article generation imports + composes this org's real generated "
                    "component library (article_component_library) in a planned layout instead of inlining "
                    "generic helpers. Default off; enable per validated org."
                ),
            ),
        ),
    ]
