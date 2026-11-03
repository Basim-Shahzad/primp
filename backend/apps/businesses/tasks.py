import hashlib
import hmac
import json
import logging
import time

import requests
from celery import shared_task
from django.conf import settings

from .models import Business

logger = logging.getLogger(__name__)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


@shared_task(
    queue="bulk",
    autoretry_for=(requests.RequestException,),
    retry_backoff=2,
    retry_backoff_max=60,
    retry_kwargs={"max_retries": 3},
)
def revalidate_frontend(business_id: int) -> bool:
    """POST a signed revalidation request to the frontend. No-op when not configured."""
    url = settings.FRONTEND_REVALIDATE_URL
    secret = settings.FRONTEND_REVALIDATE_SECRET
    if not url or not secret:
        return False
    business = (
        Business.objects.filter(pk=business_id)
        .select_related("city")
        .only("public_id", "slug", "city__slug")
        .first()
    )
    if business is None:
        return False
    body = json.dumps(
        {
            "type": "business.updated",
            "business_id": str(business.public_id),
            "slug": business.slug,
            "city": business.city.slug,
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    response = requests.post(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Primp-Timestamp": timestamp,
            "X-Primp-Signature": sign(secret, timestamp, body),
        },
        timeout=5,
    )
    response.raise_for_status()
    return True
