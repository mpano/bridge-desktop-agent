"""Menu-bar voice shares the owned API; workers never call AppKit."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from uuid import uuid4

import httpx

from app.config.settings import Settings
from app.desktop.service import LocalService, ServiceState
from app.voice.errors import VoiceError
from app.voice.paths import wake_word_model_path
from app.voice.service import VoiceEvent, VoiceService


class VoiceState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    LISTENING = "listening"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    READY = "ready"
    APPROVAL = "approval"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class VoiceStatus:
    state: VoiceState
    message: str
    transcript: str = ""
    response: str = ""
    result_status: str = ""


IDLE_STATES = {VoiceState.STOPPED, VoiceState.READY, VoiceState.APPROVAL, VoiceState.FAILED}


@dataclass(frozen=True)
class ApprovalView:
    review_id: str
    action: str
    arguments: str
    message: str
    expires_at: float


@dataclass(frozen=True)
class VoiceApproval:
    view: ApprovalView
    token: str = field(repr=False)


class LocalAPIAgent:
    """The server remains the sole workflow owner, including approvals/cancellation."""

    def __init__(self, service: LocalService, token: str, stop: threading.Event | None = None):
        self.service = service
        self.token = token
        self.stop = stop or threading.Event()
        self.source = "panel"

    async def message(self, text: str) -> dict:
        return await self._submit("/api/v1/tasks", {"message": text})

    async def confirm(self, token: str, approved: bool) -> dict:
        return await self._submit("/api/v1/tasks/confirm", {"token": token, "approved": approved})

    async def _submit(self, endpoint: str, payload: dict) -> dict:
        status = self.service.status
        if status.state != ServiceState.RUNNING or not status.url:
            raise VoiceError("Bridge local service is not ready for voice commands.")
        headers = {"Authorization": f"Bearer {self.token}", "X-Bridge-Source": self.source}
        async with httpx.AsyncClient(
            base_url=status.url,
            headers=headers,
            timeout=httpx.Timeout(15.0),
            trust_env=False,
        ) as client:
            if self.stop.is_set():
                return {"status": "cancelled", "message": "Voice stopped before submission."}
            response = await client.post(endpoint, json=payload)
            if response.status_code == 409:
                raise VoiceError("Bridge is busy. Review the active request in the dashboard.")
            if response.status_code == 401:
                raise VoiceError("Voice authentication failed. Restart Bridge with its API_TOKEN.")
            if response.status_code >= 400:
                raise VoiceError("Bridge could not submit the voice request. Open the dashboard.")
            request_id = response.json()["request_id"]
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if self.stop.is_set():
                    cancelled = await client.post(f"/api/v1/tasks/{request_id}/cancel")
                    if cancelled.status_code >= 400:
                        raise VoiceError(
                            "Could not cancel the voice task. Review it in the dashboard."
                        )
                    return {
                        "status": "cancelled",
                        "message": "Stop requested. Completed actions are not undone; "
                        "review the dashboard.",
                    }
                progress = await client.get(f"/api/v1/tasks/{request_id}")
                if progress.status_code >= 400:
                    raise VoiceError(
                        "Bridge lost the voice task. Review the dashboard before retrying."
                    )
                payload = progress.json()
                if payload.get("result") is not None:
                    return payload["result"]
                if payload.get("status") in {"failed", "cancelled"}:
                    raise VoiceError("Voice task stopped. Review its details in the dashboard.")
                await asyncio.sleep(0.2)
        raise VoiceError("Voice request timed out. Review tasks in Bridge before retrying.")


class MenuVoiceService:
    """One explicitly started microphone session, with thread-safe presentation state."""

    def __init__(self, settings: Settings, local_service: LocalService):
        self.settings = settings
        self.local_service = local_service
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._voice: VoiceService | None = None
        self._stop = threading.Event()
        self._once = False
        self._approval: VoiceApproval | None = None
        self._history: list[dict] = []
        self._status = VoiceStatus(VoiceState.STOPPED, "Ready when you are. The microphone is off.")
        # Set by the menu bar: notify when a typed request finishes while the panel is hidden.
        self.notifier: Callable[[str, str], Awaitable[None]] | None = None
        self.panel_visible = True
        self.wake_phrase = (
            "Hey Bridge" if settings.voice_wake_word_model_path is None else "your model’s phrase"
        )

    @property
    def status(self) -> VoiceStatus:
        with self._guard:
            return self._status

    @property
    def active(self) -> bool:
        with self._guard:
            return self._thread is not None and self._thread.is_alive()

    @property
    def wake_enabled(self) -> bool:
        with self._guard:
            return bool(self._thread and self._thread.is_alive() and not self._once)

    @property
    def wake_model_ready(self) -> bool:
        return wake_word_model_path(self.settings.voice_wake_word_model_path).is_file()

    @property
    def pending_review(self) -> ApprovalView | None:
        with self._guard:
            if self._approval is not None and self._approval.view.expires_at > time.monotonic():
                return self._approval.view
            return None

    def confirm_review(self, review_id: str, approved: bool) -> bool:
        """Only an explicit UI decision can consume this locally captured review."""
        with self._guard:
            if self._thread and self._thread.is_alive():
                return False
            pending = self._approval
            if pending is None or pending.view.review_id != review_id:
                return False
            if pending.view.expires_at <= time.monotonic():
                self._approval = None
                self._status = replace(
                    self._status,
                    state=VoiceState.FAILED,
                    message="This approval expired. Review the workflow in the dashboard.",
                )
                return False
            self._approval = None  # Prevent duplicate clicks and retries of uncertain submissions.
            self._stop.clear()
            self._once = True
            self._status = replace(
                self._status,
                state=VoiceState.PROCESSING,
                message="Submitting your approval…" if approved else "Declining the action…",
            )
            self._thread = threading.Thread(
                target=lambda: self._run_confirmation(pending.token, approved),
                name="bridge-voice-confirmation",
                daemon=False,
            )
            self._thread.start()
            return True

    def _run_confirmation(self, token: str, approved: bool) -> None:
        async def submit():
            client = LocalAPIAgent(
                self.local_service, self.settings.api_token.get_secret_value(), self._stop
            )
            result = await client.confirm(token, approved)
            await self._on_result(result)
            self._update(
                VoiceState.APPROVAL if self.pending_review else VoiceState.READY,
                "Another action needs your review."
                if self.pending_review
                else "Microphone off. Your decision has been processed.",
            )

        try:
            asyncio.run(submit())
        except Exception:
            self._update(
                VoiceState.FAILED,
                "Could not finish this approval. Review the dashboard before retrying; "
                "the decision may already have been received.",
            )

    def submit_text(self, text: str) -> bool:
        """Run a typed request through the same local API, approvals and status as voice."""
        text = " ".join(text.split())
        with self._guard:
            if not text or (self._thread and self._thread.is_alive()):
                return False
            if self._approval is not None and self._approval.view.expires_at > time.monotonic():
                self._status = replace(
                    self._status,
                    state=VoiceState.APPROVAL,
                    message="Review or decline the pending action before starting another.",
                )
                return False
            self._approval = None
            if not self.settings.api_token.get_secret_value():
                self._status = VoiceStatus(VoiceState.FAILED, "Set API_TOKEN before using Bridge.")
                return False
            if self.local_service.status.state in {ServiceState.STOPPED, ServiceState.FAILED}:
                self.local_service.start()
            self._stop.clear()
            self._once = True
            self._status = VoiceStatus(VoiceState.PROCESSING, "On it…", transcript=text)
            self._thread = threading.Thread(
                target=lambda: self._run_text(text), name="bridge-typed-request", daemon=False
            )
            self._thread.start()
        return True

    def _run_text(self, text: str) -> None:
        async def submit():
            await self._wait_for_service()
            if self._stop.is_set():
                self._update(VoiceState.STOPPED, "Stopped before sending.")
                return
            client = LocalAPIAgent(
                self.local_service, self.settings.api_token.get_secret_value(), self._stop
            )
            result = await self._on_result(await client.message(text))
            if result["status"] == "confirmation_required":
                self._update(VoiceState.APPROVAL, "This action needs your review.")
            elif result["status"] == "failed":
                self._update(VoiceState.READY, "That didn’t work. See the details below.")
            else:
                self._update(VoiceState.READY, "Done. Ask me anything else.")
            if self.notifier is not None and not self.panel_visible:
                if result["status"] == "confirmation_required":
                    body = "Needs your approval. Click Bridge in the menu bar to review it."
                else:
                    body = (result.get("message") or "Done.").strip()
                await self.notifier("Bridge · " + text[:60], body)

        try:
            asyncio.run(submit())
        except VoiceError as exc:
            self._update(VoiceState.FAILED, str(exc))
        except Exception:
            self._update(
                VoiceState.FAILED,
                "Could not finish this request. Review the dashboard before retrying.",
            )

    async def _wait_for_service(self) -> None:
        deadline = time.monotonic() + 15
        while not self._stop.is_set():
            status = self.local_service.status
            if status.state == ServiceState.RUNNING and status.url:
                return
            if status.state == ServiceState.FAILED:
                raise VoiceError(status.message)
            if time.monotonic() >= deadline:
                raise VoiceError("Bridge local service did not become ready. Check service status.")
            await asyncio.sleep(0.1)

    def _update(self, state: VoiceState, message: str) -> None:
        with self._guard:
            self._status = replace(self._status, state=state, message=message)

    def _on_event(self, event: VoiceEvent) -> None:
        with self._guard:
            if self._stop.is_set():
                return
            message = event.message
            if event.phase == "listening":
                cue = "the start sound" if self.settings.voice_start_sound else "‘Yes?’"
                message = f"Say {self.wake_phrase}, wait for {cue}, then give your command."
            self._status = replace(
                self._status,
                state=VoiceState(event.phase),
                message=message,
                transcript=event.transcript
                if event.transcript is not None
                else self._status.transcript,
            )

    async def _on_result(self, result: dict) -> dict:
        with self._guard:
            confirmation = result.get("confirmation")
            if result["status"] == "confirmation_required" and isinstance(confirmation, dict):
                self._approval = VoiceApproval(
                    ApprovalView(
                        uuid4().hex,
                        confirmation["action"],
                        json.dumps(confirmation["arguments"], indent=2, ensure_ascii=False),
                        result.get("message", "Review this action."),
                        time.monotonic()
                        + min(float(confirmation.get("expires_in_seconds", 300)), 300),
                    ),
                    confirmation["token"],
                )
            self._status = replace(
                self._status,
                response=result.get("message") or result["status"],
                result_status=result["status"],
            )
            self._history.append(
                {
                    "at": time.time(),
                    "request": self._status.transcript,
                    "reply": result.get("message") or result["status"],
                    "status": result["status"],
                }
            )
            del self._history[:-20]
        return result

    @property
    def history(self) -> list[dict]:
        """Recent requests from the panel (typed or spoken), newest last."""
        with self._guard:
            return list(self._history)

    def clear_history(self) -> None:
        with self._guard:
            self._history.clear()

    def train_wake_word(self) -> bool:
        """Record the user's "Hey Bridge" and retrain the detector, in the background."""
        from app.desktop.voice_training import training_unavailable

        reason = training_unavailable()
        with self._guard:
            if self._thread and self._thread.is_alive():
                return False
            if reason:
                self._status = VoiceStatus(VoiceState.FAILED, reason)
                return False
            self._stop.clear()
            self._once = True
            self._status = VoiceStatus(VoiceState.STARTING, "Let’s teach me your voice…")
            self._thread = threading.Thread(
                target=self._run_training, name="bridge-wake-training", daemon=False
            )
            self._thread.start()
        return True

    def _run_training(self) -> None:
        from app.desktop.voice_training import train

        try:
            asyncio.run(train(self._update, self._stop))
        except VoiceError as exc:
            self._update(VoiceState.FAILED, str(exc))
        except Exception:
            self._update(
                VoiceState.FAILED, "Voice training stopped unexpectedly. Please try again."
            )

    def start(self, *, once: bool = False) -> bool:
        with self._guard:
            if self._thread and self._thread.is_alive():
                return False
            if self._approval is not None:
                if self._approval.view.expires_at > time.monotonic():
                    self._status = replace(
                        self._status,
                        state=VoiceState.APPROVAL,
                        message="Review or decline the pending action "
                        "before starting another command.",
                    )
                    return False
                self._approval = None
            token = self.settings.api_token.get_secret_value()
            if not token:
                self._status = VoiceStatus(
                    VoiceState.FAILED, "Set API_TOKEN before enabling Bridge voice."
                )
                return False
            if not once and not self.wake_model_ready:
                self._status = replace(
                    self._status,
                    state=VoiceState.FAILED,
                    message="The Hey Bridge wake-word model is missing. Use Speak now "
                    "or follow the Wake-word setup guide to train and install it.",
                )
                return False
            if self.local_service.status.state in {ServiceState.STOPPED, ServiceState.FAILED}:
                self.local_service.start()
            self._stop.clear()
            self._once = once
            self._status = VoiceStatus(VoiceState.STARTING, "Preparing voice…")
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
                self._status = replace(
                    self._status,
                    state=VoiceState.STOPPING,
                    message="Stopping safely; finishing audio cleanup…",
                )

    def wait(self, timeout: float = 10) -> bool:
        thread = self._thread
        if thread:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        try:
            asyncio.run(self._listen())
        except VoiceError as exc:
            self._update(VoiceState.FAILED, str(exc))
        except Exception:
            self._update(
                VoiceState.FAILED,
                "Voice stopped unexpectedly. Check microphone permission and service status. "
                "If a command was submitted, review the dashboard before retrying.",
            )

    async def _listen(self) -> None:
        await self._wait_for_service()
        if self._stop.is_set():
            self._update(VoiceState.STOPPED, "Microphone off.")
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
        agent = LocalAPIAgent(
            self.local_service, self.settings.api_token.get_secret_value(), self._stop
        )
        agent.source = "voice"
        voice = VoiceService(
            agent, voice_settings, on_event=self._on_event, on_result=self._on_result
        )
        self._voice = voice
        # stop() can race construction. Transfer the stop flag before opening the microphone.
        if self._stop.is_set():
            voice.stop()
        pending = False
        final_message = "Microphone off. Ready for your next command."
        try:
            if self._once:
                response = await voice.listen_once()
                pending = response["status"] == "confirmation_required"
                if response.get("speech_error"):
                    final_message = response["speech_error"]
                elif response["status"] == "empty":
                    final_message = response["message"]
            else:

                async def report(result):
                    nonlocal pending
                    pending = result["status"] == "confirmation_required"
                    return await self._on_result(result)

                voice.on_result = report
                await voice.run_background()
        finally:
            try:
                await voice.close()
            finally:
                self._voice = None
        if pending:
            self._update(
                VoiceState.APPROVAL, "Microphone paused. Review this action in the dashboard."
            )
        elif self._stop.is_set():
            self._update(
                VoiceState.STOPPED, "Microphone off. Review any submitted task in the dashboard."
            )
        else:
            self._update(VoiceState.READY, final_message)
