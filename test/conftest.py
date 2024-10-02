import os

import pytest

integrated_run = os.environ.get("INTEGRATION_TEST") == "true"
lyrics_changed = os.environ.get("LYRICS_UPDATED") == "true"


def pytest_runtest_setup(item: pytest.Item):
    """Skip integration tests if INTEGRATION_TEST environment variable is not set."""
    if integrated_run and lyrics_changed:
        return None

    for marker in item.iter_markers():
        if marker.name == "integration_test" and not integrated_run:
            return pytest.skip("INTEGRATION_TEST=1 required")
        if marker.name == "on_lyrics_update" and not lyrics_changed:
            return pytest.skip("No change in lyrics source code")
