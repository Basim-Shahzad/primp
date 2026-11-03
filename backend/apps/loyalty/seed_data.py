"""Default earning rules (pure data, shared by the migration and post_migrate)."""

# (action, kind, points, unit_amount, description)
RULES: list[tuple[str, str, int, int | None, str]] = [
    ("booking", "per_amount", 10, 1000, "10 points per PKR 1,000 spent on a completed booking"),
    ("review", "flat", 25, None, "Points for reviewing a completed booking"),
    ("first_booking", "flat", 50, None, "Bonus for your first completed booking"),
    ("fifth_booking", "flat", 100, None, "Bonus for your 5th completed booking"),
    ("tenth_booking", "flat", 200, None, "Bonus for your 10th completed booking"),
    ("profile_completion", "flat", 20, None, "Bonus for completing your profile"),
]
