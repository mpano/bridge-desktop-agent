"""Opt-in real loopback lifecycle tests. No native apps or LLM requests are made."""

import asyncio
import json
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from fastapi import FastAPI

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.desktop.service import LocalService, ServiceState
from app.workflows.store import SQLiteWorkflows

pytestmark = pytest.mark.skipif(
    os.environ.get("BRIDGE_SERVICE_TESTS") != "1" and os.environ.get("DESKTOP_AGENT_SERVICE_TESTS") != "1", reason="Opt-in loopback integration test"
)


def wait_state(service, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if service.status.state in expected:
            return service.status
        time.sleep(0.02)
    pytest.fail(f"Service stuck in {service.status}")


def test_service_readiness_restart_and_database_release(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="service-test")
    runner = AsyncMock()
    llm = AsyncMock()

    def factory():
        return create_app(settings, build_agent(settings, llm=llm, runner=runner), enable_ui=True)

    service = LocalService(settings, port=0, app_factory=factory)
    try:
        assert service.start()
        status = wait_state(service, {ServiceState.RUNNING, ServiceState.FAILED})
        assert status.state == ServiceState.RUNNING
        assert status.url.startswith("http://127.0.0.1:")
        opener = build_opener(ProxyHandler({}))
        with opener.open(status.url + "/health", timeout=2) as response:
            assert json.load(response) == {"status": "ok"}
        request = Request(
            status.url + "/api/v1/projects", headers={"Authorization": "Bearer service-test"}
        )
        with opener.open(request, timeout=2) as response:
            assert json.load(response) == {"projects": []}
        assert not service.start()
        service.stop()
        assert service.wait(5)
        assert service.status.state == ServiceState.STOPPED
        store = SQLiteWorkflows(settings.database_path)
        store.close()
        assert service.start()
        assert (
            wait_state(service, {ServiceState.RUNNING, ServiceState.FAILED}).state
            == ServiceState.RUNNING
        )
        runner.run.assert_not_called()
        llm.generate_response.assert_not_called()
    finally:
        service.stop()
        assert service.wait(5)


def test_port_collision_does_not_attach_or_construct_agent(tmp_path):
    settings = Settings(_env_file=None, api_token="service-test")
    called = []
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        reserved.listen()
        service = LocalService(
            settings, port=reserved.getsockname()[1], app_factory=lambda: called.append(True)
        )
        assert service.start()
        assert service.wait(5)
        assert service.status.state == ServiceState.FAILED
        assert "Port" in service.status.message
        assert not called


def test_startup_database_conflict_is_reported(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="service-test")
    owner = SQLiteWorkflows(settings.database_path)
    service = LocalService(settings, port=0)
    try:
        assert service.start()
        assert service.wait(5)
        assert service.status.state == ServiceState.FAILED
        assert service.status.url is None
    finally:
        service.stop()
        service.wait(5)
        owner.close()


def test_stop_during_startup_runs_cleanup():
    entered = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            cleaned.set()

    service = LocalService(
        Settings(_env_file=None, api_token="service-test"),
        port=0,
        app_factory=lambda: FastAPI(lifespan=lifespan),
    )
    try:
        service.start()
        assert entered.wait(3)
        assert service.status.state == ServiceState.STARTING
        service.stop()
        release.set()
        assert service.wait(5)
        assert service.status.state == ServiceState.STOPPED
        assert cleaned.is_set()
    finally:
        release.set()
        service.stop()
        service.wait(5)
