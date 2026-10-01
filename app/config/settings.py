from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    remote_tool_results: Literal["status_only", "allowlist", "all"] = "status_only"
    remote_tool_result_allowlist: list[str] = Field(default_factory=list)
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    default_browser: str = "Google Chrome"
    default_editor: str = "Visual Studio Code"
    database_path: Path = Path("agent.db")
    screenshot_directory: Path = Path.home() / "Library/Application Support/Bridge/screenshots"
    log_level: str = "INFO"
    api_token: SecretStr = SecretStr("")
    max_rounds: int = Field(default=12, ge=1, le=50)
    integrations_enabled: bool = False
    integrations_callback_port: int = Field(default=8000, ge=1024, le=65535)
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    slack_client_id: str = ""
    spotify_client_id: str = ""
    # "Sign in with GitHub" for the dashboard (a GitHub OAuth App). Google sign-in reuses
    # GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.
    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")
    auth_session_hours: float = Field(default=12, ge=0.25, le=72)
    auth_remember_days: float = Field(default=30, ge=1, le=90)
    # Apps screen_context never reads, by name or bundle id (password managers always).
    screen_context_blocked_apps: list[str] = Field(default_factory=list)
    # Apple Shortcuts that may run without an approval prompt, by exact name.
    shortcuts_trusted: list[str] = Field(default_factory=list)
    voice_stt_provider: Literal["local", "openai"] = "local"
    voice_local_whisper_model: str = "small"
    voice_allow_model_download: bool = False
    voice_openai_stt_model: str = "gpt-transcribe"
    voice_language: str | None = "en"
    voice_record_seconds: float = Field(default=15, ge=1, le=30)
    voice_auto_stop: bool = True
    voice_silence_seconds: float = Field(default=1.0, ge=0.3, le=3)
    voice_speech_wait_seconds: float = Field(default=5, ge=1, le=15)
    voice_activity_threshold: float = Field(default=0.012, gt=0, lt=1)
    voice_start_sound: bool = True
    voice_shortcut_enabled: bool = True
    # ⌃⌥Space: act on the text selected in any app.
    text_actions_shortcut_enabled: bool = True
    voice_tts_voice: str | None = None
    voice_tts_rate: int | None = Field(default=None, ge=80, le=500)
    voice_background_enabled: bool = False
    voice_wake_word_enabled: bool = False
    voice_wake_word_model_path: Path | None = None
    voice_wake_word_threshold: float = Field(default=0.5, gt=0, le=1)

    @field_validator(
        "voice_tts_voice",
        "voice_tts_rate",
        "voice_language",
        "voice_wake_word_model_path",
        mode="before",
    )
    @classmethod
    def empty_optional_voice_setting(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("remote_tool_result_allowlist")
    @classmethod
    def tool_names(cls, values: list[str]) -> list[str]:
        if any(
            not name or len(name) > 100 or not name.replace("_", "").isalnum() for name in values
        ):
            raise ValueError("Use exact registered tool names in the remote result allowlist.")
        return list(dict.fromkeys(values))
