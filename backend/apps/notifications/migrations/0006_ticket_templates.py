# ruff: noqa: E501
"""Support ticket replies and review moderation notices (frozen copy)."""

from django.db import migrations

SMS = {
    "ticket_reply": 'Primp support replied to your request "{subject}". Open the app to read the reply.',
}
TEXT = {
    "ticket_reply": ("Support replied to your request", 'Primp support replied to "{subject}": {message}'),
    "review_moderated_author": ("Your review was updated", "Your review of {business_name} was {action} by Primp moderation. {reason}"),
    "review_moderated_business": ("A review was updated", "A review of {business_name} was {action} by Primp moderation."),
}


def forward(apps, schema_editor):
    Template = apps.get_model("notifications", "NotificationTemplate")
    for key, body in SMS.items():
        Template.objects.update_or_create(key=key, channel="sms", language="en", defaults={"subject": "", "body": body})
    for key, (subject, body) in TEXT.items():
        for channel in ("email", "in_app"):
            Template.objects.update_or_create(key=key, channel=channel, language="en", defaults={"subject": subject, "body": body})


class Migration(migrations.Migration):
    dependencies = [("notifications", "0005_admin_notices")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
