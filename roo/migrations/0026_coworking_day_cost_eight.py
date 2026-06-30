from django.db import migrations


def raise_coworking_cost(apps, schema_editor):
    RewardsCatalog = apps.get_model("roo", "RewardsCatalog")
    # Standard coworking day now costs 8 points; founders who keep their
    # monthly startup update current are charged the discounted 4 (applied in
    # CoworkingService, not in the catalog).
    RewardsCatalog.objects.filter(code="COWORKING_DAY").update(cost_points=8)


def restore_coworking_cost(apps, schema_editor):
    RewardsCatalog = apps.get_model("roo", "RewardsCatalog")
    RewardsCatalog.objects.filter(code="COWORKING_DAY").update(cost_points=4)


class Migration(migrations.Migration):
    dependencies = [
        ("roo", "0025_update_rewards_pricing_and_hide_coffee"),
    ]

    operations = [
        migrations.RunPython(raise_coworking_cost, restore_coworking_cost),
    ]
