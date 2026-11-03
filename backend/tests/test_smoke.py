"""Runs scripts/smoke_test.py against a live test server loaded with seed_demo data."""

import importlib.util
import io
from pathlib import Path

import pytest
from django.core.management import call_command

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.py"


def load_script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("smoke_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.mark.django_db(transaction=True)
def test_smoke_script_passes_against_seeded_server(live_server, capsys) -> None:  # type: ignore[no-untyped-def]
    call_command("seed_demo", "--count", "12", stdout=io.StringIO())
    load_script().run(live_server.url)
    assert "smoke test passed" in capsys.readouterr().out


def test_check_helper_raises_on_failure() -> None:
    smoke = load_script()
    smoke.check(True, "fine")
    with pytest.raises(smoke.SmokeFailure, match="broken"):
        smoke.check(False, "broken")
