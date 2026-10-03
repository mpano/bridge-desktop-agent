"""Shared test setup: nothing a test does may write to the real ~/Library."""

import pytest


@pytest.fixture(autouse=True)
def private_listening_file(tmp_path, monkeypatch):
    """The "Hey Bridge" switch is remembered in ~/Library; tests use a temporary file."""
    monkeypatch.setattr(
        "app.desktop.menubar.LISTENING_FILE", tmp_path / "listening.json", raising=False
    )
