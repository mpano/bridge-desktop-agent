"""Bridge Cloud with a fake OpenAI and a fake Lemon Squeezy. No real money, keys or requests."""

import hashlib
import hmac
import json
import time

import httpx
import pytest
from bridge_cloud.config import CloudSettings
from bridge_cloud.main import create_app
from fastapi.testclient import TestClient

SECRET = "whsec-test"
KEY = "sk-server-test"


class FakeServices:
    """OpenAI and Lemon Squeezy, answering like they would."""

    def __init__(self):
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if request.url.host == "api.lemonsqueezy.com":
            if path == "/v1/checkouts":
                return httpx.Response(
                    201,
                    json={
                        "data": {
                            "attributes": {"url": "https://bridge.lemonsqueezy.com/checkout/abc"}
                        }
                    },
                )
            if path.startswith("/v1/subscriptions/"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "attributes": {
                                "urls": {
                                    "customer_portal": "https://bridge.lemonsqueezy.com/billing/xyz"
                                }
                            }
                        }
                    },
                )
        body = (
            json.loads(request.content)
            if request.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if path == "/v1/responses" and body.get("stream"):
            events = [
                {"type": "response.output_text.delta", "delta": "Hel"},
                {"type": "response.output_text.delta", "delta": "lo"},
                {
                    "type": "response.completed",
                    "response": {"usage": {"input_tokens": 1000, "output_tokens": 100}},
                },
            ]
            text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(
                200, content=text.encode(), headers={"content-type": "text/event-stream"}
            )
        if path == "/v1/responses":
            return httpx.Response(
                200,
                json={
                    "output_text": "Hi",
                    "usage": {
                        "input_tokens": 1000,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 500,
                    },
                },
            )
        if path == "/v1/chat/completions":
            return httpx.Response(
                200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 10}}
            )
        if path == "/v1/audio/speech":
            return httpx.Response(
                200, content=b"ID3" + b"0" * 2000, headers={"content-type": "audio/mpeg"}
            )
        if path == "/v1/audio/transcriptions":
            return httpx.Response(
                200, json={"text": "hello", "usage": {"type": "duration", "seconds": 90}}
            )
        if path == "/v1/realtime/client_secrets":
            return httpx.Response(200, json={"value": "ek_test", "expires_at": 123})
        return httpx.Response(404, json={"error": {"message": "nope"}})


@pytest.fixture
def cloud(tmp_path):
    services = FakeServices()
    settings = CloudSettings(
        _env_file=None,
        database_path=tmp_path / "cloud.db",
        openai_api_key=KEY,
        lemonsqueezy_api_key="ls-test",
        lemonsqueezy_store_id="1",
        lemonsqueezy_webhook_secret=SECRET,
        plus_variant_id="100",
        topup_small_variant_id="200",
        trial_credit=0.5,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(services))
    with TestClient(create_app(settings, http=http)) as client:
        client.services = services
        client.settings = settings
        yield client


def mac(client, ip="1.2.3.4"):
    made = client.post("/v1/devices", json={"name": "MacBook"}, headers={"fly-client-ip": ip})
    assert made.status_code == 200, made.text
    data = made.json()
    return {"Authorization": f"Bearer {data['token']}"}, data


def balance(client, headers):
    return client.get("/v1/account", headers=headers).json()["balance"]


def test_a_new_mac_gets_a_small_trial(cloud):
    headers, data = mac(cloud)
    assert data["balance"] == 0.5 and data["plan"] == "trial" and data["token"].startswith("bdc_")
    assert {p["key"] for p in data["plans"]} == {"plus", "topup_small", "topup_large"}
    for _ in range(2):
        mac(cloud)
    assert (
        cloud.post("/v1/devices", json={}, headers={"fly-client-ip": "1.2.3.4"}).status_code == 429
    )
    assert cloud.get("/v1/account").status_code == 401
    assert cloud.get("/v1/account", headers={"Authorization": "Bearer nope"}).json()["error"][
        "message"
    ]


def test_answers_are_paid_from_credit_with_the_servers_key(cloud):
    headers, _ = mac(cloud)
    answer = cloud.post(
        "/v1/openai/responses", headers=headers, json={"model": "gpt-4.1-mini", "input": "hi"}
    )
    assert answer.json()["output_text"] == "Hi"
    sent = cloud.services.seen[-1]
    assert sent.headers["authorization"] == f"Bearer {KEY}"  # Never the Mac's token.
    # gpt-4.1-mini: 1000 in × $0.40/M + 500 out × $1.60/M = $0.0012.
    assert balance(cloud, headers) == pytest.approx(0.5 - 0.0012)
    refused = cloud.post(
        "/v1/openai/responses", headers=headers, json={"model": "gpt-9-secret", "input": "x"}
    )
    assert refused.status_code == 400 and "doesn't offer" in refused.json()["error"]["message"]
    assert cloud.post("/v1/openai/files", headers=headers, content=b"{}").status_code == 404


def test_streamed_answers_arrive_as_they_come_and_are_charged_at_the_end(cloud):
    headers, _ = mac(cloud)
    with cloud.stream(
        "POST",
        "/v1/openai/responses",
        headers=headers,
        json={"model": "gpt-4.1-mini", "input": "hi", "stream": True},
    ) as answer:
        text = "".join(answer.iter_text())
    assert "response.output_text.delta" in text and '"delta": "lo"' in text
    # 1000 × 0.40 + 100 × 1.60 = $0.00056.
    assert balance(cloud, headers) == pytest.approx(0.5 - 0.00056)
    cloud.post(
        "/v1/openai/chat/completions",
        headers=headers,
        json={"model": "gpt-4.1-mini", "messages": [], "stream": False},
    )
    assert "stream_options" not in json.loads(cloud.services.seen[-1].content)


def test_out_of_credit_says_how_to_get_more(cloud):
    headers, data = mac(cloud)
    store = cloud.app.state.store
    store.add(data["account"], -500_000, "usage", "use:all", "test")
    answer = cloud.post(
        "/v1/openai/responses", headers=headers, json={"model": "gpt-4.1-mini", "input": "hi"}
    )
    assert answer.status_code == 402
    assert "Settings › Plan" in answer.json()["error"]["message"]
    assert len(cloud.services.seen) == 0  # OpenAI was never asked.


def test_speech_and_transcription(cloud):
    headers, _ = mac(cloud)
    spoken = cloud.post(
        "/v1/openai/audio/speech",
        headers=headers,
        json={"model": "gpt-4o-mini-tts", "voice": "marin", "input": "x" * 1500},
    )
    assert spoken.content.startswith(b"ID3")
    assert balance(cloud, headers) == pytest.approx(0.5 - 0.03)  # 2 × 1k chars × $0.015.
    heard = cloud.post(
        "/v1/openai/audio/transcriptions",
        headers=headers,
        files={"file": ("a.m4a", b"0" * 1000)},
        data={"model": "gpt-transcribe"},
    )
    assert heard.json()["text"] == "hello"
    assert balance(cloud, headers) == pytest.approx(0.5 - 0.03 - 0.012)  # 90 s → 2 min.


def test_voice_is_charged_by_the_minute(cloud):
    headers, data = mac(cloud)
    opened = cloud.post(
        "/v1/openai/realtime/client_secrets",
        headers=headers,
        json={"session": {"type": "realtime", "model": "gpt-realtime-mini"}},
    ).json()
    assert opened["value"] == "ek_test" and opened["bridge_voice_id"].startswith("voice_")
    ended = cloud.post(
        "/v1/voice/end", headers=headers, json={"voice_id": opened["bridge_voice_id"]}
    ).json()
    assert ended == {"charged": 0.1, "minutes": 1}
    assert (
        cloud.post(
            "/v1/voice/end", headers=headers, json={"voice_id": opened["bridge_voice_id"]}
        ).json()["charged"]
        == 0
    )
    # A conversation nobody ended is charged in full (30 min) the next time.
    other = cloud.post(
        "/v1/openai/realtime/client_secrets",
        headers=headers,
        json={"session": {"model": "gpt-realtime-mini"}},
    ).json()
    store = cloud.app.state.store
    store._db().execute(
        "UPDATE voice SET started = ? WHERE id = ?", (time.time() - 3600, other["bridge_voice_id"])
    )
    assert balance(cloud, headers) == pytest.approx(0.5 - 0.1 - 3.0)
    unknown = cloud.post(
        "/v1/openai/realtime/client_secrets", headers=headers, json={"session": {"model": "gpt-x"}}
    )
    assert unknown.status_code == 400


def webhook(client, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/v1/webhooks/lemonsqueezy",
        content=body,
        headers={"x-signature": signature, "content-type": "application/json"},
    )


def test_paying_for_a_plan_and_top_ups(cloud):
    headers, data = mac(cloud)
    account = data["account"]
    custom = {"custom_data": {"account_id": account}}
    assert (
        webhook(cloud, {"meta": {"event_name": "subscription_created"}}, secret="wrong").status_code
        == 401
    )
    made = webhook(
        cloud,
        {
            "meta": {"event_name": "subscription_created", **custom},
            "data": {
                "type": "subscriptions",
                "id": "77",
                "attributes": {
                    "status": "active",
                    "customer_id": 5,
                    "renews_at": "2026-11-14T00:00:00Z",
                },
            },
        },
    )
    assert made.json()["result"] == "subscription active"
    paid = {
        "meta": {"event_name": "subscription_payment_success"},
        "data": {
            "type": "subscription-invoices",
            "id": "inv1",
            "attributes": {"subscription_id": 77, "status": "paid"},
        },
    }
    assert webhook(cloud, paid).json()["result"] == "monthly credit"
    assert webhook(cloud, paid).json()["result"] == "duplicate"  # Delivered twice: credited once.
    overview = cloud.get("/v1/account", headers=headers).json()
    assert overview["plan"] == "plus" and overview["balance"] == pytest.approx(6.5)
    topup = {
        "meta": {"event_name": "order_created", **custom},
        "data": {
            "type": "orders",
            "id": "o9",
            "attributes": {"status": "paid", "first_order_item": {"variant_id": 200}},
        },
    }
    assert webhook(cloud, topup).json()["result"] == "top-up"
    assert balance(cloud, headers) == pytest.approx(10.0)
    refund = {**topup, "meta": {"event_name": "order_refunded", **custom}}
    webhook(cloud, refund)
    assert balance(cloud, headers) == pytest.approx(6.5)
    ended = webhook(
        cloud,
        {
            "meta": {"event_name": "subscription_expired", **custom},
            "data": {"type": "subscriptions", "id": "77", "attributes": {"status": "expired"}},
        },
    )
    assert ended.json()["result"] == "subscription expired"
    assert cloud.get("/v1/account", headers=headers).json()["plan"] == "free"
    test_card = {
        "meta": {"event_name": "order_created", "test_mode": True, **custom},
        "data": topup["data"],
    }
    assert webhook(cloud, test_card).json()["result"] == "test payment ignored"


def test_checkout_portal_and_another_mac(cloud):
    headers, data = mac(cloud)
    url = cloud.post("/v1/checkout", headers=headers, json={"plan": "plus"}).json()["url"]
    assert url.startswith("https://bridge.lemonsqueezy.com/checkout/")
    sent = json.loads(cloud.services.seen[-1].content)["data"]
    assert sent["attributes"]["checkout_data"]["custom"]["account_id"] == data["account"]
    assert sent["relationships"]["variant"]["data"]["id"] == "100"
    assert (
        cloud.post("/v1/checkout", headers=headers, json={"plan": "topup_large"}).status_code == 409
    )  # Not for sale yet.
    assert cloud.post("/v1/portal", headers=headers).status_code == 409  # No subscription yet.
    custom = {"custom_data": {"account_id": data["account"]}}
    webhook(
        cloud,
        {
            "meta": {"event_name": "subscription_created", **custom},
            "data": {"type": "subscriptions", "id": "77", "attributes": {"status": "active"}},
        },
    )
    assert cloud.post("/v1/portal", headers=headers).json()["url"].endswith("/billing/xyz")
    webhook(
        cloud,
        {
            "meta": {"event_name": "license_key_created", **custom},
            "data": {"type": "license-keys", "id": "1", "attributes": {"key": "LICENSE-KEY-1234"}},
        },
    )
    second, _ = mac(cloud, ip="5.6.7.8")
    assert (
        cloud.post("/v1/link", headers=second, json={"license_key": "WRONG-KEY-0000"}).status_code
        == 404
    )
    linked = cloud.post("/v1/link", headers=second, json={"license_key": "LICENSE-KEY-1234"}).json()
    assert linked["account"] == data["account"] and linked["devices"] == 2
    cloud.post("/v1/sign-out", headers=second)
    assert cloud.get("/v1/account", headers=second).status_code == 401
