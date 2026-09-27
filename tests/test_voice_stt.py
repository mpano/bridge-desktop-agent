import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.voice.stt import LocalWhisperSTT, OpenAIWhisperSTT


@pytest.mark.parametrize("allow_download", [False, True])
@pytest.mark.asyncio
async def test_local_weights_offline_default(monkeypatch, tmp_path, allow_download):
    model = Mock()
    model.transcribe.return_value = ([SimpleNamespace(text=" hello ")], None)
    factory = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=factory))
    stt = LocalWhisperSTT(allow_download=allow_download)
    assert await stt.transcribe(tmp_path / "audio.wav") == "hello"
    factory.assert_called_once_with(
        "small", device="auto", compute_type="int8", local_files_only=not allow_download
    )
    await stt.close()
    assert stt._model is None
    with pytest.raises(RuntimeError, match="closed"):
        await stt.transcribe(tmp_path / "audio.wav")


@pytest.mark.asyncio
async def test_local_load_failure_sanitized(monkeypatch, tmp_path):
    factory = Mock(side_effect=ValueError("secret /private/path"))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=factory))
    with pytest.raises(RuntimeError, match="VOICE_ALLOW_MODEL_DOWNLOAD") as error:
        await LocalWhisperSTT().transcribe(tmp_path / "audio.wav")
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_local_cancellation_waits_for_worker_and_close(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio")
    stt = LocalWhisperSTT()

    def transcribe(audio_path):
        started.set()
        assert release.wait(5)
        assert audio_path.read_bytes() == b"audio"
        return "hello"

    monkeypatch.setattr(stt, "_transcribe", transcribe)
    task = asyncio.create_task(stt.transcribe(path))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        closing = asyncio.create_task(stt.close())
        await asyncio.sleep(0)
        assert not task.done()
        assert not closing.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await closing


@pytest.mark.asyncio
async def test_remote_closes_client_and_audio_and_limits_retries(monkeypatch, tmp_path):
    create = AsyncMock(return_value=SimpleNamespace(text=" hello "))
    client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)), close=AsyncMock()
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr("openai.AsyncOpenAI", factory)
    stt = OpenAIWhisperSTT("fake-key", language=None)
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio")
    assert await stt.transcribe(path) == "hello"
    factory.assert_called_once_with(api_key="fake-key", timeout=60.0, max_retries=0)
    arguments = create.call_args.kwargs
    assert arguments["file"].closed
    assert "language" not in arguments
    assert arguments["model"] == "gpt-transcribe"
    create.side_effect = RuntimeError("private provider response")
    with pytest.raises(RuntimeError, match="Remote speech transcription failed") as error:
        await stt.transcribe(path)
    assert "private" not in str(error.value)
    await stt.close()
    await stt.close()
    client.close.assert_awaited_once()
    with pytest.raises(RuntimeError, match="closed"):
        await stt.transcribe(path)
