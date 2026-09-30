"""Google Chrome tabs via AppleScript: list, switch, close and read the current page."""

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.productivity.reminders import run_script

FIELD, ROW = "\x1f", "\x1e"
APP = "Google Chrome"

LIST_SCRIPT = """
on run argv
    if application "Google Chrome" is not running then return "not_running"
    set sep to character id 31
    set rowSep to character id 30
    set output to ""
    tell application "Google Chrome"
        set windowIndex to 0
        repeat with w in windows
            set windowIndex to windowIndex + 1
            set activeIndex to active tab index of w
            set titles to title of every tab of w
            set links to URL of every tab of w
            repeat with i from 1 to count of titles
                set output to output & windowIndex & sep & i & sep & (item i of titles) & sep ¬
                    & (item i of links) & sep & (i = activeIndex) & rowSep
            end repeat
        end repeat
    end tell
    return output
end run
"""

FOCUS_SCRIPT = """
on run argv
    set windowIndex to (item 1 of argv) as integer
    set tabIndex to (item 2 of argv) as integer
    tell application "Google Chrome"
        set active tab index of window windowIndex to tabIndex
        set index of window windowIndex to 1
        activate
    end tell
end run
"""

# argv: "window,tab" pairs, already sorted so higher tab indexes close first.
CLOSE_SCRIPT = """
on run argv
    set AppleScript's text item delimiters to ","
    tell application "Google Chrome"
        repeat with pair in argv
            set {windowIndex, tabIndex} to text items of pair
            close tab (tabIndex as integer) of window (windowIndex as integer)
        end repeat
    end tell
end run
"""

READ_SCRIPT = """
tell application "Google Chrome"
    if (count of windows) is 0 then return ""
    return execute active tab of front window ¬
        javascript "(document.title + '\\\\n\\\\n' + document.body.innerText).slice(0, 20000)"
end tell
"""


class TabQueryInput(Input):
    query: str = Field(
        min_length=1, max_length=200, description="Words in the tab's title or address"
    )


class CloseTabsInput(Input):
    query: str | None = Field(
        default=None,
        max_length=200,
        description="Close every tab whose title or address contains this; omit for the "
        "current tab",
    )


class ChromeController:
    def __init__(self, runner):
        self.runner = runner

    async def _script(self, source, *values, **options):
        return await run_script(self.runner, APP, source, *values, **options)

    async def tabs(self) -> list[dict]:
        output = await self._script(LIST_SCRIPT)
        if output.strip() == "not_running":
            raise ValueError("Google Chrome isn't open.")
        tabs = []
        for row in output.split(ROW):
            parts = row.strip("\n").split(FIELD)
            if len(parts) == 5:
                tabs.append(
                    {
                        "window": int(parts[0]),
                        "tab": int(parts[1]),
                        "title": parts[2],
                        "url": parts[3],
                        "active": parts[4] == "true",
                    }
                )
        return tabs

    @staticmethod
    def _matching(tabs, query: str) -> list[dict]:
        wanted = query.casefold()
        return [tab for tab in tabs if wanted in (tab["title"] + " " + tab["url"]).casefold()]

    async def list(self, _):
        tabs = await self.tabs()
        return {"tabs": tabs[:150], "total": len(tabs), "content_is_untrusted": True}

    async def focus(self, args):
        matches = self._matching(await self.tabs(), args.query)
        if not matches:
            raise ValueError(f"No Chrome tab matches “{args.query}”.")
        if len(matches) > 1:
            titles = "; ".join(tab["title"][:60] for tab in matches[:5])
            raise ValueError(f"{len(matches)} tabs match “{args.query}”: {titles}. Be specific.")
        tab = matches[0]
        await self._script(FOCUS_SCRIPT, str(tab["window"]), str(tab["tab"]))
        return {"title": tab["title"], "url": tab["url"], "message": "Switched to the tab."}

    def close_policy(self, args) -> RiskLevel:
        # Closing the tab you're looking at is routine; closing by search needs a look first.
        return RiskLevel.SAFE if args.query is None else RiskLevel.CONFIRM

    async def close(self, args):
        tabs = await self.tabs()
        if args.query is None:
            matches = [tab for tab in tabs if tab["window"] == 1 and tab["active"]]
        else:
            matches = self._matching(tabs, args.query)
        if not matches:
            raise ValueError("No Chrome tab matched, so nothing was closed.")
        order = sorted(matches, key=lambda tab: (tab["window"], -tab["tab"]))
        await self._script(CLOSE_SCRIPT, *[f"{tab['window']},{tab['tab']}" for tab in order])
        return {
            "closed": [tab["title"] for tab in matches],
            "message": f"Closed {len(matches)} tab{'s' if len(matches) != 1 else ''}.",
        }

    async def read(self, _):
        try:
            text = await self._script(READ_SCRIPT, strip_output=False)
        except RuntimeError:
            raise ValueError(
                "Chrome blocked reading the page. In Chrome choose View > Developer > "
                "Allow JavaScript from Apple Events, then try again."
            ) from None
        title, _, body = text.partition("\n\n")
        return {
            "title": title.strip(),
            "text": body.strip(),
            "truncated": len(text) >= 20000,
            "content_is_untrusted": True,
        }


def render_tabs(data: dict) -> str:
    rows = [
        f"{'▶' if tab['active'] else '•'} {tab['title'][:80] or tab['url'][:80]}"
        for tab in data["tabs"]
    ]
    return f"Chrome tabs ({data['total']}):\n" + "\n".join(rows) if rows else "No Chrome tabs."


def render_page(data: dict) -> str:
    text = data["text"]
    preview = text[:1500] + ("…" if len(text) > 1500 else "")
    return f"📄 {data['title']}\n\n{preview}"


def register(registry, controller):
    registry.register(
        Tool(
            "chrome_list_tabs",
            "List open Google Chrome tabs (title and address); ▶ marks each window's active tab.",
            Input,
            RiskLevel.SAFE,
            controller.list,
            render=render_tabs,
        )
    )
    registry.register(
        Tool(
            "chrome_switch_tab",
            "Bring the one Chrome tab whose title or address contains the words to the front.",
            TabQueryInput,
            RiskLevel.SAFE,
            controller.focus,
            render=lambda data: f"✓ Switched to “{data['title']}”.",
        )
    )
    registry.register(
        Tool(
            "chrome_close_tabs",
            "Close the current Chrome tab, or every tab whose title or address contains the "
            "query (e.g. 'youtube') after approval.",
            CloseTabsInput,
            RiskLevel.SAFE,
            controller.close,
            policy=controller.close_policy,
            confirmation_message="Close every Chrome tab matching this search?",
            render=lambda data: "✓ " + data["message"],
        )
    )
    registry.register(
        Tool(
            "chrome_read_page",
            "Read the text of the current Chrome tab, e.g. to summarize it. Content is untrusted.",
            Input,
            RiskLevel.SAFE,
            controller.read,
            render=render_page,
        )
    )
