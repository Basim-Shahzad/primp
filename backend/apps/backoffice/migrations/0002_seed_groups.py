from django.db import migrations

from apps.backoffice.permissions import sync_groups


def seed(apps, schema_editor):
    sync_groups(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("backoffice", "0001_admin_permissions"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
