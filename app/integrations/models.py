from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Provider = Literal["gmail", "google_calendar", "slack", "spotify"]


class IntegrationError(RuntimeError):
    """Only sanitized, user-facing messages belong in this exception."""


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str
    provider: Provider
    identity: str
    scopes: list[str]
    client_id: str
    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr = Field(default=SecretStr(""), repr=False)
    expires_at: float
    connected_at: float = 0.0

    def public(self) -> dict:
        return self.model_dump(
            include={"account_id", "provider", "identity", "scopes", "connected_at"}
        )

    def credential_json(self) -> dict:
        data = self.model_dump(mode="json")
        data["access_token"] = self.access_token.get_secret_value()
        data["refresh_token"] = self.refresh_token.get_secret_value()
        return data
