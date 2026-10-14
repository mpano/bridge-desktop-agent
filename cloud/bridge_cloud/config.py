"""Settings, from environment variables (fly secrets / fly.toml on Fly.io)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Plan(BaseModel):
    """One thing people can buy: a monthly plan, or a one-off top-up."""

    key: str
    name: str
    price: str  # As shown, e.g. "$12 / month".
    variant_id: str = ""  # The Lemon Squeezy variant.
    credit: float  # AI credit in dollars of OpenAI cost (per month for a plan).
    monthly: bool = True


class CloudSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")
    openai_api_key: SecretStr = SecretStr("")
    openai_base: str = "https://api.openai.com/v1"
    database_path: Path = Path("cloud.db")
    public_url: str = "http://localhost:8080"
    lemonsqueezy_api_key: SecretStr = SecretStr("")
    lemonsqueezy_store_id: str = ""
    lemonsqueezy_webhook_secret: SecretStr = SecretStr("")
    # Lemon Squeezy's test mode (test cards): on while you try things, off when you launch.
    accept_test_payments: bool = False
    # Variant ids of the products made in Lemon Squeezy (Store › Products).
    plus_variant_id: str = ""
    topup_small_variant_id: str = ""
    topup_large_variant_id: str = ""
    # What a new Mac gets to try Bridge, in dollars of OpenAI cost.
    trial_credit: float = Field(default=0.50, ge=0, le=20)
    trials_per_ip_per_day: int = Field(default=3, ge=1, le=100)
    # Voice conversations, per started minute.
    voice_minute: dict[str, float] = Field(
        default_factory=lambda: {"gpt-realtime-2.1": 0.30, "gpt-realtime-mini": 0.10}
    )
    voice_max_minutes: int = 30
    speech_per_1k_chars: float = 0.015  # Text to speech (gpt-4o-mini-tts, rounded up).
    transcribe_per_minute: float = 0.006
    requests_per_minute: int = Field(default=90, ge=1)
    max_body_bytes: int = 25_000_000

    def plans(self) -> list[Plan]:
        return [
            Plan(
                key="plus",
                name="Bridge Plus",
                price="$12 / month",
                variant_id=self.plus_variant_id,
                credit=6.0,
                monthly=True,
            ),
            Plan(
                key="topup_small",
                name="$5 of AI credit",
                price="$5",
                variant_id=self.topup_small_variant_id,
                credit=3.5,
                monthly=False,
            ),
            Plan(
                key="topup_large",
                name="$20 of AI credit",
                price="$20",
                variant_id=self.topup_large_variant_id,
                credit=15.0,
                monthly=False,
            ),
        ]

    def plan(self, key: str = "", variant_id: str = "") -> Plan | None:
        for plan in self.plans():
            if (key and plan.key == key) or (variant_id and plan.variant_id == str(variant_id)):
                return plan
        return None
