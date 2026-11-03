from django.db import migrations

from apps.notifications.seed_data import template_rows


def seed(apps, schema_editor):
    Template = apps.get_model("notifications", "NotificationTemplate")
    Template.objects.bulk_create([Template(**row) for row in template_rows()], ignore_conflicts=True)


def unseed(apps, schema_editor):
    apps.get_model("notifications", "NotificationTemplate").objects.filter(language="en").delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
