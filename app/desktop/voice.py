"""Menu-bar owned voice listener that reuses the running localhost Bridge API."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.config.settings import Settings
from app.desktop.service import LocalService, ServiceState
from app.voice.paths import wake_word_model_path
from app.voice.service import VoiceService


class VoiceState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    LISTENING = "listening"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class VoiceStatus:
    state: VoiceState
    message: str


class LocalAPIAgent:
    """Minimal Agent-compatible client; the server remains the sole workflow owner."""

    def __init__(self, service: LocalService, token: str):
        self.service = service
        self.token = token

    async def message(self, text: str) -> dict:
        status = self.service.status
        if status.state != ServiceState.RUNNING or not status.url:
            raise RuntimeError("Bridge local service is not ready for voice commands.")
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(
            base_url=status.url,
            headers=headers,
            timeout=httpx.Timeout(15.0),
            trust_env=False,
        ) as client:
            response = await client.post("/api/v1/tasks", json={"message": text})
            if response.status_code == 409:
                raise RuntimeError("Bridge is busy with another request.")
            if response.status_code >= 400:
                raise RuntimeError("Bridge could not submit the voice request.")
            request_id = response.json()["request_id"]
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                progress = await client.get(f"/api/v1/tasks/{request_id}")
                if progress.status_code >= 400:
                    raise RuntimeError("Bridge lost the voice task.")
                payload = progress.json()
                if payload.get("result") is not None:
                    return payload["result"]
                await asyncio.sleep(0.2)
        raise RuntimeError("Voice request timed out. Review tasks in Bridge.")


class MenuVoiceService:
    """Nonblocking menu-bar controller for explicitly requested wake-word listening."""

    def __init__(self, settings: Settings, local_service: LocalService):
        self.settings = settings
        self.local_service = local_service
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._voice: VoiceService | None = None
        self._stop = threading.Event()
        self._status = VoiceStatus(VoiceState.STOPPED, "Voice listening is off")

    @property
    def status(self) -> VoiceStatus:
        with self._guard:
            return self._status

    def _update(self, state: VoiceState, message: str) -> None:
        with self._guard:
            self._status = VoiceStatus(state, message)

    def start(self) -> bool:
        with self._guard:
            if self._thread and self._thread.is_alive():
                return False
            token = self.settings.api_token.get_secret_value()
            if not token:
                self._status = VoiceStatus(
                    VoiceState.FAILED, "Set API_TOKEN before enabling Bridge voice."
                )
                return False
            model_path = wake_word_model_path(self.settings.voice_wake_word_model_path)
            if not model_path.is_file():
                self._status = VoiceStatus(
                    VoiceState.FAILED,
                    f'Install the custom "Bridge" wake-word model first: {model_path}',
                )
                return False
            if self.local_service.status.state in {ServiceState.STOPPED, ServiceState.FAILED}:
                self.local_service.start()
            self._stop.clear()
            self._status = VoiceStatus(
                VoiceState.STARTING, 'Starting “Bridge” wake-word listening…'
            )
            self._thread = threading.Thread(
                target=self._run, name="bridge-voice-service", daemon=False
            )
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        voice = self._voice
        if voice is not None:
            voice.stop()
        with self._guard:
            if self._thread and self._thread.is_alive():
                self._status = VoiceStatus(VoiceState.STOPPING, "Stopping voice listening…")

    def wait(self, timeout: float = 10) -> bool:
        thread = self._thread
        if thread:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        try:
            asyncio.run(self._listen())
        except (RuntimeError, OSError):
            self._update(
                VoiceState.FAILED,
                "Voice listener stopped. Check microphone permission, wake-word setup, "
                "and the local Bridge service.",
            )

    async def _listen(self) -> None:
        deadline = time.monotonic() + 15
        while not self._stop.is_set():
            status = self.local_service.status
            if status.state == ServiceState.RUNNING and status.url:
                break
            if status.state == ServiceState.FAILED:
                raise RuntimeError("Bridge local service failed to start.")
            if time.monotonic() >= deadline:
                raise RuntimeError("Bridge local service did not become ready.")
            await asyncio.sleep(0.1)
        if self._stop.is_set():
            self._update(VoiceState.STOPPED, "Voice listening is off")
            return

        voice_settings = self.settings.model_copy(
            update={
                "voice_background_enabled": True,
                "voice_wake_word_enabled": True,
                "voice_wake_word_model_path": wake_word_model_path(
                    self.settings.voice_wake_word_model_path
                ),
            }
        )
        agent = LocalAPIAgent(self.local_service, self.settings.api_token.get_secret_value())
        voice = VoiceService(agent, voice_settings)
        self._voice = voice
        self._update(VoiceState.LISTENING, 'Listening for “Bridge”')
        try:
            await voice.run_background()
        finally:
            await voice.close()
            self._voice = None
            if self.status.state != VoiceState.FAILED:
                self._update(VoiceState.STOPPED, "Voice listening is off")
