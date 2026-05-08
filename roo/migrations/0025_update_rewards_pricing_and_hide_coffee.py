from django.db import migrations


def update_rewards(apps, schema_editor):
    RewardsCatalog = apps.get_model("roo", "RewardsCatalog")

    reward_updates = {
        "EVENT_TICKET": {"cost_points": 6},
        "WORKSHOP_50": {"cost_points": 24},
        "WORKSHOP_FREE": {"cost_points": 42, "stock_remaining": 5},
        "COFFEE": {"is_active": False},
    }

    for code, updates in reward_updates.items():
        RewardsCatalog.objects.filter(code=code).update(**updates)


def restore_rewards(apps, schema_editor):
    RewardsCatalog = apps.get_model("roo", "RewardsCatalog")

    reward_updates = {
        "EVENT_TICKET": {"cost_points": 12},
        "WORKSHOP_50": {"cost_points": 30},
        "WORKSHOP_FREE": {"cost_points": 48, "stock_remaining": 5},
        "COFFEE": {"is_active": True},
    }

    for code, updates in reward_updates.items():
        RewardsCatalog.objects.filter(code=code).update(**updates)


class Migration(migrations.Migration):
    dependencies = [
        ("roo", "0024_pointspurchase"),
    ]

    operations = [
        migrations.RunPython(update_rewards, restore_rewards),
    ]
