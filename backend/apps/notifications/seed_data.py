# ruff: noqa: E501
"""Default notification templates (pure data; used by the migration and post_migrate)."""

CUSTOMER_SMS = {
    "otp": "Your Primp verification code is {code}. It expires in 5 minutes.",
    "booking_received": "Primp: {business_name} received your booking for {services} on {date} at {time}. We will confirm shortly.",
    "booking_confirmed": "Primp: confirmed! {services} at {business_name}, {date} at {time}.",
    "booking_rescheduled": "Primp: your booking at {business_name} moved to {date} at {time}.",
    "booking_cancelled_by_business": "Primp: {business_name} cancelled your booking on {date} at {time}.{note_suffix} Sorry about that.",
    "booking_rescheduled_by_business": "Primp: {business_name} moved your booking to {date} at {time}. Sorry for the change.",
    "reschedule_request_accepted": "Primp: {business_name} accepted your new time: {date} at {time}.",
    "reschedule_request_declined": "Primp: {business_name} could not move your booking; it stays on {date} at {time}.",
    "reminder_24h": "Reminder: {services} at {business_name} tomorrow, {date} at {time}.",
    "reminder_2h": "Reminder: {services} at {business_name} today at {time}. See you soon!",
    "review_prompt": "How was {business_name}? Rate your visit on Primp.",
    "onboarding_status_changed": "Primp: {business_name} is now {status}. {message}",
    "new_booking": "Primp: new booking from {customer_name}: {services} on {date} at {time}.",
    "booking_cancelled_by_customer": "Primp: {customer_name} cancelled {services} on {date} at {time}{late_note}.",
    "provider_booking_rescheduled": "Primp: {customer_name} moved {services} to {date} at {time}.",
    "provider_reschedule_requested": "Primp: {customer_name} asks to move {services} to {date} at {time}. Please respond.",
    "new_review": "Primp: {customer_name} left a {rating}-star review for {business_name}.",
    "pending_escalation": "Primp: {customer_name} is waiting for you to accept {services} on {date} at {time}. Please respond.",
    "business_suspended_notice": "Primp: {business_name} is temporarily unavailable. Your booking on {date} at {time} may be affected; please contact support if you need help.",
    "ticket_reply": 'Primp support replied to your request "{subject}". Open the app to read the reply.',
    "booking_reassigned": "Primp: your {services} at {business_name} on {date} at {time} is now with {staff_name}.",
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
    "booking_cancelled_by_business": (
        "Your booking was cancelled",
        "Hi {customer_name}, {business_name} cancelled your booking on {date} at {time}.{note_suffix} We are sorry for the inconvenience.",
    ),
    "booking_rescheduled_by_business": (
        "Your booking was moved",
        "Hi {customer_name}, {business_name} moved your booking to {date} at {time}. We are sorry for the change.",
    ),
    "reschedule_request_accepted": (
        "Your new time was accepted",
        "Hi {customer_name}, {business_name} accepted your request. Your booking is now on {date} at {time}.",
    ),
    "reschedule_request_declined": (
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
    "business_suspended_notice": (
        "Your booking may be affected",
        "Hi {customer_name}, {business_name} is temporarily unavailable. Your booking on {date} at {time} may be affected. Please contact Primp support if you need help.",
    ),
    "customer_suspended_notice": (
        "A customer was suspended",
        "The customer booked for {services} on {date} at {time} has been suspended by Primp support. Contact support if you have questions.",
    ),
    "ticket_reply": (
        "Support replied to your request",
        'Primp support replied to "{subject}": {message}',
    ),
    "review_moderated_author": (
        "Your review was updated",
        "Your review of {business_name} was {action} by Primp moderation. {reason}",
    ),
    "review_moderated_business": (
        "A review was updated",
        "A review of {business_name} was {action} by Primp moderation.",
    ),
    "pending_escalation": (
        "A booking is waiting for you",
        "{customer_name} is waiting for you to accept {services} on {date} at {time}. Please accept or decline it.",
    ),
    "booking_reassigned": (
        "Your appointment has a new staff member",
        "Hi {customer_name}, your {services} at {business_name} on {date} at {time} will now be with {staff_name}.",
    ),
    "onboarding_status_changed": (
        "Your business status changed",
        "{business_name} is now {status}. {message}",
    ),
    "new_review": (
        "New review",
        "{customer_name} left a {rating}-star review for {business_name}.",
    ),
    "new_booking": (
        "New booking",
        "{customer_name} booked {services} on {date} at {time}.",
    ),
    "booking_cancelled_by_customer": (
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
