"""A Bridge plan instead of an OpenAI key: the app against the real Bridge Cloud code, in
memory, with fake OpenAI and Lemon Squeezy. Nothing leaves this Mac; no Keychain."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from bridge_cloud.config import CloudSettings  # noqa: E402
from bridge_cloud.lemon import Lemon  # noqa: E402
from bridge_cloud.main import create_app as create_cloud  # noqa: E402

from app.config.settings import Settings  # noqa: E402
from app.integrations.cloud import Cloud, CloudError  # noqa: E402
from app.llm import endpoint  # noqa: E402


class Vault:
    def __init__(self):
        self.token = ""

    def load(self):
        return self.token

    def save(self, token):
        self.token = token

    def clear(self):
        self.token = ""


def fake_openai(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.lemonsqueezy.com":
        return httpx.Response(
            201, json={"data": {"attributes": {"url": "https://shop.example/checkout"}}}
        )
    if request.url.path == "/v1/realtime/client_secrets":
        return httpx.Response(200, json={"value": "ek_1", "expires_at": 1})
    return httpx.Response(
        200, json={"output_text": "OK", "usage": {"input_tokens": 10, "output_tokens": 2}}
    )


@pytest.fixture
def world(tmp_path):
    """Bridge Cloud (in memory) and a Mac's settings pointed at it."""
    cloud_settings = CloudSettings(
        _env_file=None,
        database_path=tmp_path / "cloud.db",
        openai_api_key="sk-server",
        lemonsqueezy_api_key="ls",
        lemonsqueezy_store_id="1",
        plus_variant_id="100",
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(fake_openai))
    server = create_cloud(cloud_settings, http=upstream)
    server.state.http = upstream
    server.state.lemon = Lemon(cloud_settings, server.state.store, upstream)
    to_cloud = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://cloud.test"
    )
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        api_token="token",
        cloud_url="http://cloud.test",
    )
    env = tmp_path / ".env"
    env.write_text("API_TOKEN=token\n")
    vault = Vault()
    return SimpleNamespace(
        settings=settings,
        cloud=Cloud(settings, vault=vault, http=to_cloud, env=env),
        vault=vault,
        env=env,
        server=server,
        http=to_cloud,
    )


def test_requests_go_to_openai_or_to_the_plan():
    settings = Settings(
        _env_file=None, openai_api_key="sk-mine", cloud_url="https://cloud.example/"
    )
    assert (
        endpoint.base(settings) == "https://api.openai.com/v1"
        and endpoint.key(settings) == "sk-mine"
    )
    settings.ai_provider, settings.cloud_token = "bridge", SecretStr("bdc_x")
    assert endpoint.url(settings, "responses") == "https://cloud.example/v1/openai/responses"
    assert endpoint.headers(settings) == {"Authorization": "Bearer bdc_x"}


async def test_a_free_trial_then_buying_plus(world):
    started = await world.cloud.start()
    assert started["balance"] == 0.5 and "token" not in started
    assert world.vault.token.startswith("bdc_") and world.settings.ai_provider == "bridge"
    assert "AI_PROVIDER=bridge" in world.env.read_text()
    assert (await world.cloud.overview())["plan"] == "trial"
    assert await world.cloud.checkout("plus") == "https://shop.example/checkout"
    with pytest.raises(CloudError, match="no subscription"):
        await world.cloud.portal()
    with pytest.raises(CloudError, match="isn't known"):
        await world.cloud.link("NOT-A-REAL-KEY")
    await world.cloud.sign_out()
    assert world.vault.token == "" and world.settings.ai_provider == "own_key"
    assert await world.cloud.overview() is None


async def test_the_ai_client_follows_the_plan(world):
    from app.llm.client import OpenAILLMClient

    world.settings.openai_api_key = SecretStr("sk-mine")
    client = OpenAILLMClient(world.settings)
    first = client._openai()
    assert str(first.base_url).startswith("https://api.openai.com") and first.api_key == "sk-mine"
    await world.cloud.start()
    second = client._openai()
    assert (
        str(second.base_url) == "http://cloud.test/v1/openai/"
        and second.api_key == world.vault.token
    )


async def test_voice_with_a_plan_is_opened_by_the_cloud_and_charged_when_it_ends(world):
    from app.assistant.talk import Talk

    await world.cloud.start()
    world.settings.talk_enabled = True
    world.settings.talk_model = "gpt-realtime-mini"  # The best one needs more than a trial.
    agent = SimpleNamespace(
        executor=SimpleNamespace(registry=SimpleNamespace(definitions=lambda: [])), planner=None
    )
    talk = Talk(world.settings.database_path, agent, world.settings, http=world.http)
    talk.tools = lambda from_phone: []
    made = await talk.start(from_phone=False)
    assert made["client_secret"] == "ek_1"
    talk.end(made["session_id"], 30)
    [voice_id] = talk.ended_voice
    before = (await world.cloud.overview())["balance"]
    await world.cloud.voice_ended(voice_id)
    assert (await world.cloud.overview())["balance"] == pytest.approx(before - 0.1)


async def test_health_says_when_the_credit_is_low_or_gone(world):
    from app.api.health import plan_check

    await world.cloud.start()
    assert (await plan_check(world.cloud))["state"] == "ok"
    store = world.server.state.store
    account = (await world.cloud.overview())["account"]
    store.add(account, -400_000, "usage", "use:a")
    low = await plan_check(world.cloud)
    assert low["state"] == "warn" and "$0.10" in low["detail"]
    store.add(account, -200_000, "usage", "use:b")
    gone = await plan_check(world.cloud)
    assert gone["state"] == "error" and gone["fix"]["section"] == "plan"


def test_plan_routes(world):
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.bootstrap import build_agent

    agent = build_agent(world.settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    agent.cloud = world.cloud
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(world.settings, agent, enable_ui=True)) as client:
        assert client.get("/api/v1/plan", headers=headers).json()["account"] is None
        started = client.post("/api/v1/plan/start", headers=headers).json()
        assert started["provider"] == "bridge" and started["account"]["balance"] == 0.5
        assert client.post("/api/v1/plan/checkout", headers=headers, json={"plan": "plus"}).json()[
            "url"
        ]
        assert (
            client.post(
                "/api/v1/plan/checkout", headers=headers, json={"plan": "free-money"}
            ).status_code
            == 422
        )
        # Back to your own key needs a key.
        assert client.post("/api/v1/plan/own-key", headers=headers).status_code == 409
        assert (
            client.post(
                "/api/v1/plan/link", headers=headers, json={"license_key": "UNKNOWN-KEY"}
            ).status_code
            == 409
        )
        assert client.get("/api/v1/plan").status_code == 401
