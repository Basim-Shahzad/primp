import io
import statistics
import time

import pytest
from django.core.management import call_command
from django.test import Client

pytestmark = [pytest.mark.slow, pytest.mark.django_db(transaction=True)]

URL = "/api/v1/search/businesses"
QUERIES = [
    {"lat": 24.8607, "lng": 67.0011},
    {"lat": 24.9, "lng": 67.08, "radius_km": 10, "rating_min": 4, "verified_only": "true"},
    {"lat": 24.86, "lng": 67.0, "radius_km": 3, "q": "glow"},
    {"lat": 24.87, "lng": 67.05, "sort": "rating", "gender_type": "women", "price_max": 3000},
    {"lat": 24.82, "lng": 67.03, "radius_km": 5, "category_id": 1, "sort": "price"},
]


def test_p95_under_300ms_on_5000_businesses() -> None:
    call_command("seed_demo", "--count", "5000", stdout=io.StringIO())
    client = Client()
    for params in QUERIES:  # warm connections, plans and the next-slot cache
        client.get(URL, params)
    timings = []
    for _ in range(8):
        for params in QUERIES:
            start = time.perf_counter()
            r = client.get(URL, {**params, "limit": 20})
            timings.append((time.perf_counter() - start) * 1000)
            assert r.status_code == 200
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    print(f"search p50={statistics.median(timings):.0f}ms p95={p95:.0f}ms n={len(timings)}")
    assert p95 < 300
