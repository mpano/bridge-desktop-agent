"""The OpenAI API key in the Mac's Keychain, so it never has to live in a file."""

from __future__ import annotations

from pydantic import SecretStr

SERVICE, ACCOUNT = "Bridge OpenAI", "api-key"
loaded_from = ""  # "env" or "keychain", as decided at startup


def _backend():
    import keyring

    return keyring


def read_key() -> str:
    try:
        return _backend().get_password(SERVICE, ACCOUNT) or ""
    except Exception:
        return ""


def save_key(value: str) -> None:
    _backend().set_password(SERVICE, ACCOUNT, value)


def load_into(settings) -> str:
    """Use the Keychain key when .env has none. Returns where the key came from."""
    global loaded_from
    if settings.openai_api_key.get_secret_value():
        loaded_from = "env"
    elif key := read_key():
        settings.openai_api_key = SecretStr(key)
        loaded_from = "keychain"
    return loaded_from
