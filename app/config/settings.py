from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    llm_provider: Literal["openai", "ollama"] = "openai"
    local_llm_base_url: str = "http://127.0.0.1:11434"
    local_llm_model: str = Field(default="qwen3:8b", min_length=1, max_length=200)
    local_llm_timeout_seconds: float = Field(default=120, ge=1, le=600)
    remote_tool_results: Literal["status_only", "allowlist", "all"] = "status_only"
    remote_tool_result_allowlist: list[str] = Field(default_factory=list)
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    default_browser: str = "Google Chrome"
    default_editor: str = "Visual Studio Code"
    database_path: Path = Path("agent.db")
    screenshot_directory: Path = (
        Path.home() / "Library/Application Support/Bridge/screenshots"
    )
    log_level: str = "INFO"
    api_token: SecretStr = SecretStr("")
    max_rounds: int = Field(default=12, ge=1, le=50)
    voice_stt_provider: Literal["local", "openai"] = "local"
    voice_local_whisper_model: str = "small"
    voice_allow_model_download: bool = False
    voice_openai_stt_model: str = "gpt-transcribe"
    voice_language: str | None = "en"
    voice_record_seconds: float = Field(default=6, ge=1, le=30)
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

    @field_validator("local_llm_base_url")
    @classmethod
    def local_url(cls, value: str) -> str:
        from app.llm.ollama import validate_local_url

        return validate_local_url(value)

    @field_validator("local_llm_model")
    @classmethod
    def local_model(cls, value: str) -> str:
        if not value.strip() or "cloud" in value.lower() or any(c.isspace() for c in value):
            raise ValueError("Select a locally installed model without a cloud tag.")
        return value

    @field_validator("remote_tool_result_allowlist")
    @classmethod
    def tool_names(cls, values: list[str]) -> list[str]:
        if any(
            not name or len(name) > 100 or not name.replace("_", "").isalnum() for name in values
        ):
            raise ValueError("Use exact registered tool names in the remote result allowlist.")
        return list(dict.fromkeys(values))
