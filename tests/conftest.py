"""pytest fixtures - registra plugin asyncio."""
import os
import pytest

# Forza una directory temp per i token-file durante test
os.environ.setdefault("ASTROARCH_TOKEN", "test-token")
os.environ.setdefault("ASTROARCH_LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
