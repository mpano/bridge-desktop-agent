"""Own one local ASGI server without moving AppKit off the main thread."""

import asyncio
import errno
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import uvicorn
from fastapi import FastAPI

from app.api.server import create_app
from app.config.settings import Settings


class ServiceState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class ServiceStatus:
    state: ServiceState
    message: str
    url: str | None = None


class LocalService:
    """Start/stop are nonblocking; the worker owns the socket, event loop, and agent."""

    def __init__(
        self, settings: Settings, port: int = 8000, app_factory: Callable[[], FastAPI] | None = None
    ):
        if not 0 <= port <= 65535:
            raise ValueError("Invalid service port.")
        self.settings = settings
        self.port = port
        self._factory = app_factory or (lambda: create_app(settings, enable_ui=True))
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = ServiceStatus(ServiceState.STOPPED, "Service stopped")

    @property
    def status(self) -> ServiceStatus:
        with self._guard:
            return self._status

    def _update(self, state: ServiceState, message: str, url: str | None = None) -> None:
        with self._guard:
            if state == ServiceState.RUNNING and self._stop.is_set():
                return
            self._status = ServiceStatus(state, message, url)

    def start(self) -> bool:
        with self._guard:
            if self._thread and self._thread.is_alive():
                return False
            if not self.settings.api_token.get_secret_value():
                self._status = ServiceStatus(
                    ServiceState.FAILED,
                    "Set API_TOKEN in your .env file, then relaunch Desktop Agent.",
                )
                return False
            self._stop.clear()
            self._status = ServiceStatus(ServiceState.STARTING, "Starting local service…")
            self._thread = threading.Thread(
                target=self._run, name="desktop-agent-service", daemon=False
            )
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._guard:
            if self._thread and self._thread.is_alive():
                self._status = ServiceStatus(
                    ServiceState.STOPPING, "Stopping; finishing active requests (up to 30 seconds)…"
                )

    def wait(self, timeout: float = 35) -> bool:
        """For final cleanup/tests only; never block a menu callback with this."""
        thread = self._thread
        if thread:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except OSError as exc:
            message = (
                "Port is already in use. Stop the other CLI/API/launcher first."
                if exc.errno == errno.EADDRINUSE
                else "Cannot bind the local service. Check macOS network permissions."
            )
            self._update(ServiceState.FAILED, message)
        except (Exception, SystemExit):
            # Never put configuration objects, credentials, or arbitrary exception text in the menu.
            self._update(
                ServiceState.FAILED,
                "Service could not start or stopped unexpectedly. Check configuration and "
                "close other agents using this database. Try --ui in Terminal for diagnostics.",
            )

    async def _serve(self) -> None:
        # Reserve our own socket before constructing the agent. Never probe or attach to a stranger.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(128)
            listener.setblocking(False)
            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            server = uvicorn.Server(
                uvicorn.Config(
                    self._factory(),
                    host="127.0.0.1",
                    port=self.port,
                    access_log=False,
                    log_level="warning",
                    timeout_graceful_shutdown=30,
                    lifespan="on",
                    proxy_headers=False,
                )
            )

            async def serve_owned():
                try:
                    await server.serve(sockets=[listener])
                except SystemExit as exc:
                    # Uvicorn exits on lifespan failure. Convert inside the task so it
                    # cannot unwind asyncio.run before our owner retrieves the result.
                    raise RuntimeError("Local service startup failed.") from exc

            task = asyncio.create_task(serve_owned())
            try:
                while not task.done():
                    if self._stop.is_set():
                        server.should_exit = True
                    elif server.started and self.status.state == ServiceState.STARTING:
                        self._update(ServiceState.RUNNING, "Local service running", url)
                    await asyncio.sleep(0.05)
                await task
                if not server.started and not self._stop.is_set():
                    raise RuntimeError("Service failed during startup.")
            finally:
                if not task.done():
                    server.should_exit = True
                    await task
        self._update(ServiceState.STOPPED, "Service stopped")
