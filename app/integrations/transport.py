"""Bounded provider requests, no redirects, automatic retries or raw error bodies."""

import httpx

from app.integrations.models import IntegrationError


class ProviderHTTP:
    def __init__(self, transport=None):
        self.transport = transport

    async def request(self, method: str, url: str, *, token: str | None = None, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(
                timeout=20, follow_redirects=False, trust_env=False, transport=self.transport
            ) as client:
                async with client.stream(method, url, headers=headers, **kwargs) as response:
                    if response.status_code == 401:
                        raise IntegrationError(
                            "Account authorization expired or was revoked. Reconnect in "
                            "Connections."
                        )
                    if response.status_code == 403:
                        raise IntegrationError(
                            "Provider denied access. Check granted scopes, account eligibility, and"
                            " workspace policy."
                        )
                    if response.status_code == 429:
                        raise IntegrationError(
                            "Provider rate limit reached. No retry was made; wait before trying "
                            "again."
                        )
                    if response.status_code == 404 and "api.spotify.com/v1/me/player" in url:
                        raise IntegrationError(
                            "No active Spotify device. Open Spotify on this Mac or your phone, "
                            "start any song, then try again."
                        )
                    if not 200 <= response.status_code < 300:
                        raise IntegrationError(
                            "Provider rejected the request. Check identifiers and account setup. "
                            "For writes, verify the provider before retrying."
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 2_000_000:
                            raise IntegrationError(
                                "Provider response is too large. Narrow the requested data."
                            )
                    if not content:
                        return {}
                    import json

                    data = json.loads(content)
                    if not isinstance(data, dict):
                        raise ValueError
                    if data.get("ok") is False:
                        raise IntegrationError(
                            "Slack rejected the request. Check scopes, workspace access, and "
                            "destination. No retry was made."
                        )
                    return data
        except IntegrationError:
            raise
        except (httpx.HTTPError, ValueError):
            raise IntegrationError(
                "Provider response was unavailable or invalid. A submitted write may have "
                "succeeded; check the service before retrying. Bridge did not retry."
            ) from None
