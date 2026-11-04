# ruff: noqa: E501
"""Notices sent when an admin suspends an account (frozen copy)."""

from django.db import migrations

SMS = {
    "business_suspended_notice": "Primp: {business_name} is temporarily unavailable. Your booking on {date} at {time} may be affected; please contact support if you need help.",
}
TEXT = {
    "business_suspended_notice": ("Your booking may be affected", "Hi {customer_name}, {business_name} is temporarily unavailable. Your booking on {date} at {time} may be affected. Please contact Primp support if you need help."),
    "customer_suspended_notice": ("A customer was suspended", "The customer booked for {services} on {date} at {time} has been suspended by Primp support. Contact support if you have questions."),
}


def forward(apps, schema_editor):
    Template = apps.get_model("notifications", "NotificationTemplate")
    for key, body in SMS.items():
        Template.objects.update_or_create(key=key, channel="sms", language="en", defaults={"subject": "", "body": body})
    for key, (subject, body) in TEXT.items():
        for channel in ("email", "in_app"):
            Template.objects.update_or_create(key=key, channel=channel, language="en", defaults={"subject": subject, "body": body})


class Migration(migrations.Migration):
    dependencies = [("notifications", "0004_provider_templates")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
