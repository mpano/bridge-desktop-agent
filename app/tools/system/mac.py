"""Mac status (battery, storage, network) and a few safe controls (appearance, display sleep)."""

import re
import shutil
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.productivity.reminders import run_script

APPEARANCE = """
on run argv
    set mode to item 1 of argv
    tell application "System Events"
        tell appearance preferences
            if mode is "dark" then
                set dark mode to true
            else if mode is "light" then
                set dark mode to false
            else
                set dark mode to not dark mode
            end if
            return dark mode
        end tell
    end tell
end run
"""


class MacControlInput(Input):
    action: Literal["dark_mode", "light_mode", "toggle_appearance", "sleep_display", "sleep"] = (
        Field(description="sleep_display locks the screen if a password is required after sleep")
    )


class MacController:
    def __init__(self, runner, home: Path | None = None):
        self.runner = runner
        self.home = home or Path.home()

    async def status(self, _):
        battery = await self.runner.run("/usr/bin/pmset", "-g", "batt")
        percent = re.search(r"(\d+)%", battery)
        remaining = re.search(r"(\d+:\d+) remaining", battery)
        power = "AC Power" in battery.splitlines()[0] if battery else False
        state = re.search(r"%;\s*([a-zA-Z ]+);", battery)
        disk = shutil.disk_usage(self.home)
        try:
            address = await self.runner.run("/usr/sbin/ipconfig", "getifaddr", "en0")
        except RuntimeError:
            address = ""
        return {
            "battery_percent": int(percent.group(1)) if percent else None,
            "battery_state": state.group(1).strip() if state else None,
            "on_power_adapter": power,
            "time_remaining": remaining.group(1) if remaining else None,
            "disk_free_gb": round(disk.free / 1e9, 1),
            "disk_total_gb": round(disk.total / 1e9, 1),
            "wifi_connected": bool(address.strip()),
        }

    def policy(self, args) -> RiskLevel:
        # Sleeping the whole Mac interrupts everything running; ask first.
        return RiskLevel.CONFIRM if args.action == "sleep" else RiskLevel.SAFE

    async def control(self, args):
        if args.action in {"dark_mode", "light_mode", "toggle_appearance"}:
            mode = {"dark_mode": "dark", "light_mode": "light"}.get(args.action, "toggle")
            dark = await run_script(self.runner, "System Events", APPEARANCE, mode)
            label = "Dark" if dark.strip() == "true" else "Light"
            return {"action": args.action, "message": f"{label} mode is on."}
        if args.action == "sleep_display":
            await self.runner.run("/usr/bin/pmset", "displaysleepnow")
            return {"action": args.action, "message": "Display is going to sleep."}
        await self.runner.run("/usr/bin/pmset", "sleepnow")
        return {"action": args.action, "message": "Mac is going to sleep."}


def render_status(data: dict) -> str:
    lines = []
    if data["battery_percent"] is not None:
        source = "charging" if data["on_power_adapter"] else "on battery"
        if data["battery_state"] == "charged":
            source = "fully charged"
        extra = f", {data['time_remaining']} left" if data["time_remaining"] else ""
        lines.append(f"🔋 Battery {data['battery_percent']}% ({source}{extra})")
    lines.append(f"💾 {data['disk_free_gb']} GB free of {data['disk_total_gb']} GB")
    lines.append("📶 Wi-Fi connected" if data["wifi_connected"] else "📶 Wi-Fi not connected")
    return "\n".join(lines)


def register(registry, controller):
    registry.register(
        Tool(
            "mac_status",
            "Show battery level and charging, free disk space and Wi-Fi connection.",
            Input,
            RiskLevel.SAFE,
            controller.status,
            render=render_status,
        )
    )
    registry.register(
        Tool(
            "mac_control",
            "Switch dark/light mode, put the display to sleep (locks the screen), or sleep "
            "the Mac after approval. For Focus/Do Not Disturb use an Apple Shortcut.",
            MacControlInput,
            RiskLevel.SAFE,
            controller.control,
            policy=controller.policy,
            confirmation_message="Put your Mac to sleep now? Running work will pause.",
            render=lambda data: "✓ " + data["message"],
        )
    )
