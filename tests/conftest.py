"""Shared test setup: nothing a test does may write to the real ~/Library."""

import pytest


@pytest.fixture(autouse=True)
def private_listening_file(tmp_path, monkeypatch):
    """The "Hey Bridge" switch is remembered in ~/Library; tests use a temporary file."""
    monkeypatch.setattr(
        "app.desktop.menubar.LISTENING_FILE", tmp_path / "listening.json", raising=False
    )


@pytest.fixture(autouse=True)
def no_real_work_accounts(monkeypatch):
    """Tests never see your real Jira/GitHub sign-ins or your git copies."""
    from app.integrations import repos, tokens

    store: dict = {}
    monkeypatch.setattr(tokens.KeychainTokens, "load", lambda self: dict(store))
    monkeypatch.setattr(tokens.KeychainTokens, "save", lambda self, data: store.update(data))
    monkeypatch.setattr(tokens, "GH_CANDIDATES", ())
    monkeypatch.setattr(repos, "find_copies", lambda home, extra=(), depth=3: {})
