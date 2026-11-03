from django.db import transaction
from django.dispatch import Signal

profile_updated = Signal()  # sends user_id


def emit_profile_updated(user_id: int) -> None:
    transaction.on_commit(lambda: profile_updated.send(sender=None, user_id=user_id))
