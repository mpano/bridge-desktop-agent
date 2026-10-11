"""Talk with Bridge: a real-time, interruptible voice conversation (OpenAI Realtime).

The page (the Mac's talk panel, or Ask on the phone) connects straight to OpenAI over WebRTC
with a short-lived key minted here, so audio streams both ways with the browser's own echo
cancellation: you can interrupt Bridge mid-sentence. Your audio goes to OpenAI only while a
conversation is open, and only after you turn Conversation mode on.

Bridge's tools run here, on the Mac, through the same agent as Ask: the same checks, the same
privacy filter on what the model may see, and the same approvals. Anything that sends or
changes something waits for your yes — said out loud, or tapped on the card.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import httpx

from app.llm import prompts
from app.phone.access import MAC_ONLY_TOOLS

log = logging.getLogger(__name__)
CLIENT_SECRETS = "https://api.openai.com/v1/realtime/client_secrets"
MODELS = ("gpt-realtime-2.1", "gpt-realtime-mini")
VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
MAX_SESSIONS = 4
SESSION_SECONDS = 30 * 60  # A conversation longer than this needs a new one.
# Tools a conversation doesn't need (the model sees fewer, answers faster).
SKIPPED = {"prune_workflow_records", "set_preferences", "get_preferences", "list_installed_apps"}
VOICE_RULES = """
You are now talking with the user out loud, in a live voice conversation.
- Speak naturally and briefly: one or two short sentences, like a sharp colleague. No lists,
  no markdown, no reading out links, IDs or long numbers unless asked.
- If the user interrupts, stop and follow the new direction.
- Use tools to look things up and to act; say a few words first if it may take a moment
  ("Let me check").
- When a tool result says "needs_approval", say in one sentence exactly what will happen and
  ask if you should go ahead. Only when the user clearly says yes, call approve_pending with
  approve=true; if they say no or hesitate, approve=false. Never claim something was sent or
  changed until the tool says it succeeded.
- When a result says "shown_on_screen", the details are private: say they're on the screen,
  don't make them up.
- When the user says goodbye, thanks that's all, or asks you to stop listening, say a short
  goodbye and call end_conversation.
"""
EXTRA_TOOLS = [
    {
        "type": "function",
        "name": "approve_pending",
        "description": "Answer the action that's waiting for the user's OK, after they said "
        "yes (approve=true) or no (approve=false) out loud.",
        "parameters": {
            "type": "object",
            "properties": {"approve": {"type": "boolean"}},
            "required": ["approve"],
        },
    },
    {
        "type": "function",
        "name": "end_conversation",
        "description": "End the voice conversation when the user is done.",
        "parameters": {"type": "object", "properties": {}},
    },
]


class TalkError(RuntimeError):
    """Safe to show to the user."""


class Talk:
    def __init__(self, path: Path | str, agent, settings, http: httpx.AsyncClient | None = None):
        self.agent, self.settings, self.http = agent, settings, http
        self.path = str(path)
        self.sessions: dict[str, dict] = {}
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS talk_usage (month TEXT PRIMARY KEY, seconds REAL)"
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    # Settings --------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "talk_enabled", False))

    def usage(self) -> dict:
        month = datetime.now().strftime("%Y-%m")
        with self._db() as db:
            row = db.execute("SELECT seconds FROM talk_usage WHERE month = ?", (month,)).fetchone()
        return {"month": month, "minutes": round((row[0] if row else 0) / 60, 1)}

    # Starting a conversation ---------------------------------------------------------------

    def tools(self, from_phone: bool) -> list[dict]:
        registry = self.agent.executor.registry
        listed = []
        for tool in registry.tools():
            if not tool.expose_to_llm or tool.name in SKIPPED:
                continue
            if from_phone and tool.name in MAC_ONLY_TOOLS:
                continue
            definition = tool.definition()
            listed.append(
                {
                    "type": "function",
                    "name": definition["name"],
                    "description": definition["description"][:1000],
                    "parameters": definition["parameters"],
                }
            )
        return listed + EXTRA_TOOLS

    def instructions(self, name: str, from_phone: bool) -> str:
        where = (
            "The user is on their phone, away from the Mac: you can't see their screen."
            if from_phone
            else "The user is at their Mac; 'this' means what's on their screen."
        )
        who = f"The user's name is {name}." if name else ""
        return f"{prompts.system_prompt()}\n{VOICE_RULES}\n{where} {who}".strip()

    async def start(self, *, from_phone: bool, name: str = "") -> dict:
        if not self.enabled:
            raise TalkError("Turn on Conversation mode in Settings › Voice first.")
        key = self.settings.openai_api_key.get_secret_value()
        if not key:
            raise TalkError("Add your OpenAI key in Settings first.")
        now = time.time()
        self.sessions = {
            k: v
            for k, v in self.sessions.items()
            if not v["ended"] and now - v["started"] < SESSION_SECONDS
        }
        if len(self.sessions) >= MAX_SESSIONS:
            raise TalkError("Too many conversations are open. End one first.")
        model = getattr(self.settings, "talk_model", MODELS[0])
        voice = getattr(self.settings, "talk_voice", VOICES[0])
        body = {
            "expires_after": {"anchor": "created_at", "seconds": 120},
            "session": {
                "type": "realtime",
                "model": model,
                "instructions": self.instructions(name, from_phone),
                "audio": {
                    "input": {
                        "noise_reduction": {
                            "type": "far_field" if not from_phone else "near_field"
                        },
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "auto",
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    },
                    "output": {"voice": voice},
                },
                "tools": self.tools(from_phone),
                "tool_choice": "auto",
            },
        }
        client = self.http or httpx.AsyncClient(timeout=20)
        try:
            response = await client.post(
                CLIENT_SECRETS, headers={"Authorization": f"Bearer {key}"}, json=body
            )
        except httpx.HTTPError:
            raise TalkError("Couldn't reach OpenAI. Check your connection.") from None
        finally:
            if self.http is None:
                await client.aclose()
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str((response.json().get("error") or {}).get("message", ""))[:200]
            except ValueError:
                pass
            log.warning("Realtime session refused: %s %s", response.status_code, detail)
            raise TalkError(detail or "OpenAI didn't start the conversation. Try again.")
        data = response.json()
        session_id = secrets.token_urlsafe(16)
        self.sessions[session_id] = {
            "started": now,
            "from_phone": from_phone,
            "pending": None,
            "ended": False,
        }
        return {
            "session_id": session_id,
            "client_secret": data["value"],
            "expires_at": data.get("expires_at"),
            "model": model,
            "calls_url": "https://api.openai.com/v1/realtime/calls",
        }

    def _session(self, session_id: str) -> dict:
        session = self.sessions.get(session_id)
        if session is None or session["ended"]:
            raise TalkError("This conversation has ended. Start a new one.")
        return session

    # Running Bridge's tools for the model -------------------------------------------------

    def _for_model(self, response: dict) -> tuple[dict, str]:
        """What the model may see (privacy rules apply) and what the screen shows."""
        privacy = self.agent.planner.privacy
        status = response.get("status")
        if status == "confirmation_required":
            confirmation = response.get("confirmation") or {}
            return (
                {
                    "needs_approval": True,
                    "action": response.get("message") or confirmation.get("action"),
                    "details": confirmation.get("arguments"),
                },
                response.get("message") or "",
            )
        if status != "completed":
            return {"success": False, "error": response.get("message") or status}, ""
        step = (response.get("steps") or [{}])[-1]
        seen = privacy.filter_result(step)
        display = step.get("display") or ""
        if seen.get("result_withheld"):
            return {"success": True, "shown_on_screen": bool(display)}, display
        return {"success": True, "result": seen}, display

    async def tool(self, session_id: str, name: str, arguments: dict) -> dict:
        session = self._session(session_id)
        if name == "approve_pending":
            return await self.approve(session_id, bool(arguments.get("approve")))
        if session["from_phone"] and name in MAC_ONLY_TOOLS:
            return {
                "for_model": {"success": False, "error": "That only works at the Mac."},
                "display": "",
            }
        if session["pending"]:
            return {
                "for_model": {
                    "success": False,
                    "error": "Another action is waiting for the user's yes or no. Ask first.",
                },
                "display": "",
            }
        try:
            response = await self.agent.request_tool(name, arguments)
        except (ValueError, KeyError) as exc:
            return {"for_model": {"success": False, "error": str(exc)[:300]}, "display": ""}
        for_model, display = self._for_model(response)
        approval = None
        if response.get("status") == "confirmation_required":
            session["pending"] = response["confirmation"]["token"]
            approval = {"message": display, "details": response["confirmation"]["arguments"]}
        return {"for_model": for_model, "display": display, "approval": approval}

    async def approve(self, session_id: str, approved: bool) -> dict:
        session = self._session(session_id)
        token, session["pending"] = session["pending"], None
        if not token:
            return {
                "for_model": {"success": False, "error": "Nothing is waiting for approval."},
                "display": "",
            }
        try:
            response = await self.agent.confirm(token, approved)
        except ValueError as exc:
            return {"for_model": {"success": False, "error": str(exc)[:300]}, "display": ""}
        if not approved:
            return {"for_model": {"success": True, "cancelled": True}, "display": "Not done."}
        for_model, display = self._for_model(response)
        return {"for_model": for_model, "display": display or response.get("message", "")}

    # Keeping the conversation ---------------------------------------------------------------

    async def log(self, session_id: str, role: str, text: str) -> None:
        """What was said, in your chats (Ask › Recent), like a typed conversation."""
        self._session(session_id)
        text = " ".join(str(text).split())[:4000]
        if text and role in {"user", "assistant"}:
            await self.agent.log_spoken(role, text)

    def end(self, session_id: str, seconds: float) -> dict:
        session = self.sessions.get(session_id)
        if session is None or session["ended"]:
            return self.usage()
        session["ended"] = True
        if session["pending"]:
            session["pending"] = None  # It stays in Approvals for later.
        spent = max(0.0, min(float(seconds), time.time() - session["started"] + 5))
        month = datetime.now().strftime("%Y-%m")
        with self._db() as db:
            db.execute(
                "INSERT INTO talk_usage VALUES (?, ?) ON CONFLICT(month) "
                "DO UPDATE SET seconds = seconds + excluded.seconds",
                (month, spent),
            )
        return self.usage()
