from django.db import migrations

from apps.loyalty.seed_data import RULES


def seed(apps, schema_editor):
    PointRule = apps.get_model("loyalty", "PointRule")
    for action, kind, points, unit, description in RULES:
        PointRule.objects.get_or_create(
            action=action,
            defaults={"kind": kind, "points": points, "unit_amount": unit, "description": description},
        )


def unseed(apps, schema_editor):
    apps.get_model("loyalty", "PointRule").objects.filter(action__in=[r[0] for r in RULES]).delete()


class Migration(migrations.Migration):
    dependencies = [("loyalty", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
