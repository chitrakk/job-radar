"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _no_llm_throttle(monkeypatch):
    """Disable the provider RPM limiter.

    llm.py sleeps between calls to stay inside free-tier limits, which is right in
    production and pointless in tests — it turned a 3-second suite into 45 seconds.
    """
    monkeypatch.setenv("JOBRADAR_NO_THROTTLE", "1")


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """Keep a developer's real keys out of the tests.

    Without this, running the suite on a machine with GEMINI_API_KEY set would make live
    API calls and the "no key configured" tests would fail confusingly.
    """
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.delenv(var, raising=False)
