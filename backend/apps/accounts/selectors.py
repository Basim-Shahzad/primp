from .models import CustomerProfile, User


def profile_of(user: User) -> CustomerProfile | None:
    return CustomerProfile.objects.filter(user=user).first()
