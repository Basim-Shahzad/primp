from django.contrib.postgres.operations import BtreeGistExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies: list[tuple[str, str]] = []

    operations = [BtreeGistExtension(), TrigramExtension()]
