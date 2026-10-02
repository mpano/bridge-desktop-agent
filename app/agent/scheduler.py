"""Runs saved schedules while Bridge's local service is open.

Each due schedule becomes an ordinary tracked task (visible in the dashboard). Approval
rules are unchanged: a scheduled request that needs approval pauses and notifies you.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from app.workflows.schedules import Schedule, ScheduleStore

# Asleep or closed at the scheduled time? Run late runs within this grace, skip older ones.
GRACE = timedelta(hours=2)


class Scheduler:
    def __init__(
        self,
        agent,
        store: ScheduleStore,
        notify: Callable[[str, str], Awaitable[None]],
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        interval: float = 20,
    ):
        self.agent, self.store, self.notify = agent, store, notify
        self.clock, self.interval = clock, interval

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # A broken schedule must never stop the service; try again next tick.
            await asyncio.sleep(self.interval)

    async def tick(self) -> None:
        now = self.clock()
        for item in self.store.due(now):
            if now - datetime.fromisoformat(item.next_run) > GRACE:
                self.store.advance(item, now, "missed")
                continue
            try:
                progress = self.agent.submit(item.message)
            except ValueError:
                return  # Busy with another request; retry on the next tick.
            self.store.advance(item, now, "running")
            result = await self._wait(progress["request_id"])
            self.store.record(item.id, result.get("status", "failed"))
            await self._announce(item, result)
            return  # One scheduled run per tick keeps things predictable.

    async def _wait(self, request_id: str) -> dict:
        for _ in range(900):  # Up to 15 minutes.
            progress = self.agent.task_progress(request_id)
            if progress["result"] is not None:
                return progress["result"]
            await asyncio.sleep(1)
        return {"status": "failed", "message": "Still running; check the dashboard."}

    async def _announce(self, item: Schedule, result: dict) -> None:
        status = result.get("status")
        title = "Bridge · " + (item.message[:60] or "Scheduled task")
        if status == "confirmation_required":
            body = "Needs your approval. Open Bridge's dashboard → Workflows to review it."
        elif status == "completed":
            body = result.get("message") or "Done."
        else:
            body = "Didn't finish: " + (result.get("message") or "check the dashboard.")
        view = "today" if "plan my day" in title.lower() else "chat"
        await self.notify(title, body, view=view)
