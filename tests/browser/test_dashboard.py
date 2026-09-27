"""Opt-in real browser checks; network and macOS operations are replaced by test adapters."""

import asyncio
import os
import sqlite3
import threading
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.agent.context import AgentContext
from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import LLMResponse

pytestmark = pytest.mark.skipif(
    os.environ.get("BRIDGE_BROWSER_TESTS") != "1" and os.environ.get("DESKTOP_AGENT_BROWSER_TESTS") != "1", reason="Opt-in headless browser test"
)


def test_dashboard_end_to_end(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    settings = Settings(
        _env_file=None, database_path=tmp_path / "db", api_token="browser-test-token"
    )
    llm = AsyncMock()
    llm.generate_response.return_value = LLMResponse(text='<img src=x onerror="alert(1)">')
    runner = AsyncMock()
    agent = build_agent(settings, llm=llm, runner=runner)
    for index in range(26):
        agent.workflows.save(AgentContext(request_id=f"old-{index:02}"), "completed")
    with sqlite3.connect(settings.database_path) as db:
        db.execute("UPDATE workflows SET updated_at='2000-01-01 00:00:00'")
    origin = "http://127.0.0.1:8765"
    with TestClient(create_app(settings, agent, enable_ui=True), base_url=origin) as client:
        with playwright.sync_playwright() as engine:
            browser = engine.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1360, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def serve(route):
                    req = route.request
                    response = client.request(
                        req.method,
                        req.url.removeprefix(origin),
                        headers=req.headers,
                        content=req.post_data_buffer,
                    )
                    response_headers = {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() not in {"content-length", "content-encoding"}
                    }
                    route.fulfill(
                        status=response.status_code, headers=response_headers, body=response.content
                    )

                page.route(origin + "/**", serve)
                page.goto(origin)
                page.get_by_label("Local API token").fill("wrong")
                page.get_by_role("button", name="Connect", exact=True).click()
                playwright.expect(page.locator("#notice")).to_contain_text("Invalid bearer")
                page.get_by_label("Local API token").fill("browser-test-token")
                page.get_by_role("button", name="Connect", exact=True).click()
                playwright.expect(page.locator("#workspace")).to_be_visible()
                playwright.expect(page.get_by_role("button", name="Send request")).to_be_enabled()
                assert page.evaluate("localStorage.length + sessionStorage.length") == 0
                assert page.locator("#api-token").input_value() == ""
                page.screenshot(path="/private/tmp/desktop-agent-dashboard.png", full_page=True)

                page.get_by_role("button", name="Capabilities 05").click()
                playwright.expect(page.locator("#provider-details")).to_contain_text(
                    "OpenAI (remote)"
                )
                playwright.expect(page.locator("#provider-details")).to_contain_text("status_only")
                page.locator("#capability-search").fill("clipboard")
                playwright.expect(page.locator("#capability-list")).to_contain_text(
                    "read_clipboard"
                )
                playwright.expect(page.locator("#capability-list")).not_to_contain_text("open_app")
                playwright.expect(page.locator("#capability-list")).to_contain_text("CONFIRM")
                page.get_by_role("button", name="Refresh diagnostics").click()
                playwright.expect(page.locator("#diagnostics-list")).to_contain_text("macos")
                assert not llm.generate_response.called
                runner.run.assert_not_called()

                page.get_by_role("button", name="Projects 02").click()
                page.get_by_label("Project name").fill("NLP")
                page.get_by_label("Folder path").fill(str(tmp_path))
                page.get_by_role("button", name="Review and save").click()
                playwright.expect(page.get_by_role("dialog")).to_be_visible()
                assert agent.list_projects() == []
                page.get_by_role("button", name="Approve action", exact=True).click()
                playwright.expect(page.locator("#project-list")).to_contain_text("nlp")
                assert agent.list_projects()[0]["path"] == str(tmp_path)
                page.get_by_role("button", name="Forget", exact=True).click()
                page.get_by_role("button", name="Decline", exact=True).click()
                playwright.expect(page.get_by_role("dialog")).not_to_be_visible()
                assert len(agent.list_projects()) == 1

                page.get_by_role("button", name="Preferences 04").click()
                page.get_by_label("Browser", exact=True).fill("Safari")
                page.get_by_label("Project editor").fill("GoLand")
                page.get_by_role("button", name="Review changes").click()
                page.get_by_role("button", name="Approve action", exact=True).click()
                playwright.expect(page.get_by_role("dialog")).not_to_be_visible()
                assert agent.get_preferences()["default_editor"] == "GoLand"

                page.get_by_role("button", name="Workflows 03").click()
                page.get_by_role("button", name="Review steps").first.click()
                playwright.expect(page.locator("#workflow-detail")).to_contain_text(
                    "set_preferences"
                )
                page.get_by_label("Show workflows").select_option("completed")
                playwright.expect(page.get_by_role("button", name="Next page")).to_be_enabled()
                page.get_by_role("button", name="Next page").click()
                playwright.expect(page.locator("#workflow-list")).to_contain_text("old-00")
                page.locator("#workflow-list article").filter(has_text="old-00").get_by_role(
                    "button", name="Review steps"
                ).click()
                playwright.expect(page.locator("#workflow-detail")).to_contain_text("old-00")
                page.get_by_label("Keep newest terminal records").fill("0")
                page.get_by_role("button", name="Preview cleanup").click()
                playwright.expect(page.get_by_role("dialog")).to_contain_text("26 workflow")
                page.get_by_role("button", name="Decline", exact=True).click()
                playwright.expect(page.get_by_role("dialog")).not_to_be_visible()
                assert agent.workflows.get("old-00")["status"] == "completed"
                page.get_by_role("button", name="Preview cleanup").click()
                page.get_by_role("button", name="Approve action", exact=True).click()
                playwright.expect(page.locator("#workflow-detail")).to_contain_text(
                    "Record no longer available"
                )
                playwright.expect(page.get_by_role("button", name="Next page")).to_be_disabled()
                with pytest.raises(ValueError):
                    agent.workflows.get("old-00")
                page.get_by_role("button", name="Conversation 01").click()
                page.get_by_label("Your request").fill("Hello")
                page.get_by_role("button", name="Send request").click()
                playwright.expect(page.locator("#conversation")).to_contain_text("<img src=x")
                assert page.locator("#conversation img").count() == 0
                release = threading.Event()

                async def delayed_reply(*_):
                    while not release.is_set():
                        await asyncio.sleep(0.01)
                    return LLMResponse(text="This reply must be discarded after cancellation.")

                llm.generate_response.side_effect = delayed_reply
                page.get_by_label("Your request").fill("A slow request")
                page.get_by_role("button", name="Send request").click()
                playwright.expect(page.locator("#live-task")).to_be_visible()
                try:
                    page.reload()
                    page.get_by_label("Local API token").fill("browser-test-token")
                    page.get_by_role("button", name="Connect", exact=True).click()
                    playwright.expect(page.locator("#workspace")).to_be_visible()
                    page.get_by_role("button", name="Workflows 03").click()
                    page.locator("#task-list").get_by_role(
                        "button", name="Follow task"
                    ).first.click()
                    playwright.expect(page.locator("#live-task")).to_be_visible()
                    page.get_by_role("button", name="Stop remaining work").click()
                    playwright.expect(page.locator("#task-progress")).to_contain_text(
                        "Stopping after the current operation"
                    )
                finally:
                    release.set()
                playwright.expect(page.locator("#conversation")).to_contain_text(
                    "Remaining work cancelled"
                )
                playwright.expect(page.locator("#live-task")).not_to_be_visible()
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.get_by_role("button", name="Disconnect", exact=True).click()
                playwright.expect(page.locator("#connect-panel")).to_be_visible()
                playwright.expect(page.locator("#workspace")).not_to_be_visible()
                assert not errors
                runner.run.assert_not_called()
            finally:
                browser.close()
