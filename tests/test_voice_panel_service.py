import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.config.settings import Settings
from app.desktop.service import ServiceState, ServiceStatus
from app.desktop.voice import LocalAPIAgent, MenuVoiceService, VoiceState
from app.voice.errors import VoiceError
from app.voice.service import VoiceEvent


def make_service(tmp_path):
    settings = Settings(
        _env_file=None, api_token="test-token", voice_wake_word_model_path=tmp_path / "missing.onnx"
    )
    local = Mock()
    local.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    return MenuVoiceService(settings, local)


def test_speak_once_does_not_require_wake_model(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    release, started = threading.Event(), threading.Event()

    def run():
        started.set()
        release.wait(2)

    monkeypatch.setattr(service, "_run", run)
    try:
        assert service.start(once=True)
        assert started.wait(1)
        assert service._once
        assert not service.start(once=True)
        assert service.active
    finally:
        release.set()
        assert service.wait(2)


def test_default_phrase_is_hey_bridge_and_missing_model_does_not_listen(tmp_path, monkeypatch):
    from app.voice import paths

    monkeypatch.setattr(paths, "DEFAULT_WAKE_WORD_MODEL", tmp_path / "hey_bridge.onnx")
    service = make_service(tmp_path)
    service.settings.voice_wake_word_model_path = None
    service = MenuVoiceService(service.settings, service.local_service)
    assert service.wake_phrase == "Hey Bridge"
    assert not service.wake_model_ready
    assert not service.start()
    assert "Hey Bridge" in service.status.message
    assert not service.active


def test_events_keep_transcript_and_ignore_late_updates_after_stop(tmp_path):
    service = make_service(tmp_path)
    service._on_event(VoiceEvent("processing", "Working", "open Spotify"))
    service._on_event(VoiceEvent("speaking", "Opened"))
    assert service.status.transcript == "open Spotify"
    service.stop()
    previous = service.status
    service._on_event(VoiceEvent("listening", "waiting"))
    assert service.status == previous


@pytest.mark.parametrize(
    "error,expected",
    [
        (VoiceError("Install shared ONNX assets"), "Install shared ONNX assets"),
        (ValueError("private credentials"), "Voice stopped unexpectedly"),
    ],
)
def test_only_safe_errors_are_displayed(tmp_path, monkeypatch, error, expected):
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_listen", AsyncMock(side_effect=error))
    service._run()
    assert service.status.state == VoiceState.FAILED
    assert expected in service.status.message
    assert "private credentials" not in service.status.message


async def test_one_shot_results_and_approval_survive_cleanup(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service._once = True
    fake = SimpleNamespace(stop=Mock(), close=AsyncMock())

    def construct(agent, settings, *, on_event, on_result):
        async def once():
            on_event(VoiceEvent("recording", "Speak now"))
            on_event(VoiceEvent("processing", "Working", "create a folder"))
            await on_result({"status": "confirmation_required", "message": "Create reports?"})
            return {"status": "confirmation_required"}

        fake.listen_once = once
        return fake

    monkeypatch.setattr("app.desktop.voice.VoiceService", construct)
    await service._listen()
    assert service.status.state == VoiceState.APPROVAL
    assert service.status.transcript == "create a folder"
    assert service.status.response == "Create reports?"
    assert service.status.result_status == "confirmation_required"
    fake.close.assert_awaited_once()
    assert service._voice is None


async def test_listener_does_not_claim_ready_before_detector(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service._update(VoiceState.STARTING, "Preparing")
    fake = SimpleNamespace(stop=Mock(), close=AsyncMock())

    def construct(agent, settings, *, on_event, on_result):
        async def background():
            assert service.status.state == VoiceState.STARTING
            on_event(VoiceEvent("listening", "Stream ready"))
            assert service.status.state == VoiceState.LISTENING

        fake.run_background = background
        return fake

    monkeypatch.setattr("app.desktop.voice.VoiceService", construct)
    await service._listen()
    assert service.status.state == VoiceState.READY
    fake.close.assert_awaited_once()


async def test_stop_during_construction_transfers_to_voice(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service._once = True
    fake = SimpleNamespace(
        stop=Mock(), close=AsyncMock(), listen_once=AsyncMock(return_value={"status": "cancelled"})
    )

    def construct(*args, **kwargs):
        service.stop()
        return fake

    monkeypatch.setattr("app.desktop.voice.VoiceService", construct)
    await service._listen()
    fake.stop.assert_called_once()
    assert service.status.state == VoiceState.STOPPED


@pytest.mark.parametrize(
    "response,message",
    [
        ({"status": "empty", "message": "No command heard"}, "No command heard"),
        ({"status": "completed", "speech_error": "Speech output failed"}, "Speech output failed"),
    ],
)
async def test_final_notices_not_overwritten(tmp_path, monkeypatch, response, message):
    service = make_service(tmp_path)
    service._once = True
    fake = SimpleNamespace(
        stop=Mock(), close=AsyncMock(), listen_once=AsyncMock(return_value=response)
    )
    monkeypatch.setattr("app.desktop.voice.VoiceService", lambda *a, **kw: fake)
    await service._listen()
    assert service.status.message == message


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.desktop.voice.httpx.AsyncClient", client)


async def test_local_api_uses_auth_and_returns_pending_without_approval(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    paths = []

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(202, json={"request_id": "example"})
        return httpx.Response(200, json={"result": {"status": "confirmation_required"}})

    mock_http(monkeypatch, handler)
    result = await LocalAPIAgent(service.local_service, "test-token").message("make folder")
    assert result["status"] == "confirmation_required"
    assert paths == ["/api/v1/tasks", "/api/v1/tasks/example"]


async def test_stop_requests_cancellation_for_submitted_task(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stop = threading.Event()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        stop.set()
        return httpx.Response(202, json={"request_id": "example"})

    mock_http(monkeypatch, handler)
    result = await LocalAPIAgent(service.local_service, "test-token", stop).message("open Spotify")
    assert paths == ["/api/v1/tasks", "/api/v1/tasks/example/cancel"]
    assert "not undone" in result["message"]


async def test_stop_before_submission_does_not_send_request(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stop = threading.Event()
    stop.set()
    handler = Mock()
    mock_http(monkeypatch, handler)
    result = await LocalAPIAgent(service.local_service, "test-token", stop).message("open Spotify")
    assert result["status"] == "cancelled"
    handler.assert_not_called()
