# ruff: noqa: E501
"""Default notification templates (pure data; used by the migration and post_migrate)."""

CUSTOMER_SMS = {
    "otp": "Your Primp verification code is {code}. It expires in 5 minutes.",
    "booking_received": "Primp: {business_name} received your booking for {services} on {date} at {time}. We will confirm shortly.",
    "booking_confirmed": "Primp: confirmed! {services} at {business_name}, {date} at {time}.",
    "booking_rescheduled": "Primp: your booking at {business_name} moved to {date} at {time}.",
    "booking_cancelled": "Primp: {business_name} cancelled your booking on {date} at {time}. Sorry about that.",
    "booking_reschedule_declined": "Primp: {business_name} could not move your booking; it stays on {date} at {time}.",
    "reminder_24h": "Reminder: {services} at {business_name} tomorrow, {date} at {time}.",
    "reminder_2h": "Reminder: {services} at {business_name} today at {time}. See you soon!",
    "review_prompt": "How was {business_name}? Rate your visit on Primp.",
}

# (key, subject, body) used for email and in-app
EMAIL = {
    "booking_received": (
        "We received your booking",
        "Hi {customer_name}, {business_name} received your booking for {services} on {date} at {time}. You will hear from them soon.",
    ),
    "booking_confirmed": (
        "Your booking is confirmed",
        "Hi {customer_name}, your booking at {business_name} is confirmed: {services}, {date} at {time}. Total {total_price}, payable at the venue.",
    ),
    "booking_rescheduled": (
        "Your booking was rescheduled",
        "Hi {customer_name}, your booking at {business_name} is now on {date} at {time}.",
    ),
    "booking_cancelled": (
        "Your booking was cancelled",
        "Hi {customer_name}, {business_name} cancelled your booking on {date} at {time}. We are sorry for the inconvenience.",
    ),
    "booking_reschedule_declined": (
        "Reschedule request declined",
        "Hi {customer_name}, {business_name} could not accept the new time. Your booking stays on {date} at {time}.",
    ),
    "reminder_24h": (
        "Your appointment is tomorrow",
        "Hi {customer_name}, a reminder of {services} at {business_name} on {date} at {time}.",
    ),
    "reminder_2h": (
        "Your appointment is in 2 hours",
        "Hi {customer_name}, {services} at {business_name} starts at {time} today.",
    ),
    "review_prompt": (
        "How was your visit?",
        "Hi {customer_name}, tell others about {business_name}. Leave a review on Primp.",
    ),
    "provider_booking_created": (
        "New booking",
        "{customer_name} booked {services} on {date} at {time}.",
    ),
    "provider_booking_cancelled": (
        "Booking cancelled",
        "{customer_name} cancelled {services} on {date} at {time}{late_note}.",
    ),
    "provider_booking_rescheduled": (
        "Booking rescheduled",
        "{customer_name} moved {services} to {date} at {time}.",
    ),
    "provider_reschedule_requested": (
        "Reschedule request",
        "{customer_name} asks to move {services} to {date} at {time}. Please accept or decline.",
    ),
}


def template_rows() -> list[dict[str, str]]:
    rows = [
        {"key": key, "channel": "sms", "language": "en", "subject": "", "body": body}
        for key, body in CUSTOMER_SMS.items()
    ]
    for key, (subject, body) in EMAIL.items():
        for channel in ("email", "in_app"):
            rows.append(
                {"key": key, "channel": channel, "language": "en", "subject": subject, "body": body}
            )
    return rows
