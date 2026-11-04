# ruff: noqa: E501
"""Provider-side templates: rename overlapping keys and add the new ones (frozen copy)."""

from django.db import migrations

RENAMES = {
    "provider_booking_created": "new_booking",
    "provider_booking_cancelled": "booking_cancelled_by_customer",
    "booking_cancelled": "booking_cancelled_by_business",
    "booking_reschedule_declined": "reschedule_request_declined",
}

SMS = {
    "booking_cancelled_by_business": "Primp: {business_name} cancelled your booking on {date} at {time}.{note_suffix} Sorry about that.",
    "booking_rescheduled_by_business": "Primp: {business_name} moved your booking to {date} at {time}. Sorry for the change.",
    "reschedule_request_accepted": "Primp: {business_name} accepted your new time: {date} at {time}.",
    "booking_reassigned": "Primp: your {services} at {business_name} on {date} at {time} is now with {staff_name}.",
    "pending_escalation": "Primp: {customer_name} is waiting for you to accept {services} on {date} at {time}. Please respond.",
    "onboarding_status_changed": "Primp: {business_name} is now {status}. {message}",
    "new_booking": "Primp: new booking from {customer_name}: {services} on {date} at {time}.",
    "booking_cancelled_by_customer": "Primp: {customer_name} cancelled {services} on {date} at {time}{late_note}.",
    "provider_booking_rescheduled": "Primp: {customer_name} moved {services} to {date} at {time}.",
    "provider_reschedule_requested": "Primp: {customer_name} asks to move {services} to {date} at {time}. Please respond.",
    "new_review": "Primp: {customer_name} left a {rating}-star review for {business_name}.",
}

TEXT = {  # key -> (subject, body) for email and in_app
    "booking_cancelled_by_business": ("Your booking was cancelled", "Hi {customer_name}, {business_name} cancelled your booking on {date} at {time}.{note_suffix} We are sorry for the inconvenience."),
    "booking_rescheduled_by_business": ("Your booking was moved", "Hi {customer_name}, {business_name} moved your booking to {date} at {time}. We are sorry for the change."),
    "reschedule_request_accepted": ("Your new time was accepted", "Hi {customer_name}, {business_name} accepted your request. Your booking is now on {date} at {time}."),
    "booking_reassigned": ("Your appointment has a new staff member", "Hi {customer_name}, your {services} at {business_name} on {date} at {time} will now be with {staff_name}."),
    "new_review": ("New review", "{customer_name} left a {rating}-star review for {business_name}."),
    "pending_escalation": ("A booking is waiting for you", "{customer_name} is waiting for you to accept {services} on {date} at {time}. Please accept or decline it."),
    "onboarding_status_changed": ("Your business status changed", "{business_name} is now {status}. {message}"),
}


def forward(apps, schema_editor):
    Template = apps.get_model("notifications", "NotificationTemplate")
    Notification = apps.get_model("notifications", "Notification")
    for old, new in RENAMES.items():
        Template.objects.filter(key=old).update(key=new)
        Notification.objects.filter(template_key=old).update(template_key=new)
    for key, body in SMS.items():
        Template.objects.update_or_create(
            key=key, channel="sms", language="en", defaults={"subject": "", "body": body}
        )
    for key, (subject, body) in TEXT.items():
        for channel in ("email", "in_app"):
            Template.objects.update_or_create(
                key=key, channel=channel, language="en", defaults={"subject": subject, "body": body}
            )


def backward(apps, schema_editor):
    Template = apps.get_model("notifications", "NotificationTemplate")
    Notification = apps.get_model("notifications", "Notification")
    for old, new in RENAMES.items():
        Template.objects.filter(key=new).update(key=old)
        Notification.objects.filter(template_key=new).update(template_key=old)


class Migration(migrations.Migration):
    dependencies = [("notifications", "0003_owner_preferences_and_flags")]
    operations = [migrations.RunPython(forward, backward)]
