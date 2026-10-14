import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """Tests never reach a real service: a request to a real address fails the test."""
    import httpx

    real = httpx.AsyncHTTPTransport.handle_async_request

    async def check(self, request):
        host = request.url.host or ""
        if host not in LOCAL_HOSTS and not host.endswith((".test", ".localhost")):
            raise AssertionError(f"A test tried to reach {host}; use a fake.")
        return await real(self, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", check)
