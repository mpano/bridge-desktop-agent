from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.system.apps import AppInput, app_name


class URLInput(Input):
    url: str
    browser: str | None = None


class SearchInput(Input):
    query: str


class BrowserController:
    def __init__(self, runner, default_browser: str, default_provider=None):
        self.runner = runner
        self.default_browser = default_browser
        self.default_provider = default_provider

    async def open_url(self, url: str, browser: str | None = None) -> dict:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Only HTTP(S) URLs without embedded credentials are supported.")
        default = self.default_provider() if self.default_provider else self.default_browser
        selected = app_name(browser or default)
        AppInput(app_name=selected)
        await self.runner.run("/usr/bin/open", "-a", selected, url)
        return {"message": "macOS accepted opening the URL.", "url": url}

    @asynccontextmanager
    async def automation_session(self):
        """Isolated opt-in browser; never attach to personal browser profiles."""
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=False)
            try:
                yield await browser.new_context()
            finally:
                await browser.close()


def register(registry, controller):
    async def open_url(args):
        return await controller.open_url(args.url, args.browser)

    async def search(args):
        return await controller.open_url(
            "https://www.google.com/search?" + urlencode({"q": args.query})
        )

    registry.register(
        Tool(
            "open_url",
            "Open an HTTP(S) website in Chrome or another browser.",
            URLInput,
            RiskLevel.SAFE,
            open_url,
        )
    )
    registry.register(
        Tool(
            "search_web",
            "Search Google in the configured browser.",
            SearchInput,
            RiskLevel.SAFE,
            search,
        )
    )
