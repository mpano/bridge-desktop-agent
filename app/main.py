import argparse
import asyncio
import json

from app.agent.agent import Agent
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.console import read_input
from app.diagnostics import collect_diagnostics
from app.workflows.retention import RetentionPolicy, WorkflowQuery

HELP = """Commands:
  /tools [filter]   Browse registered capabilities and approval rules
  /doctor          Check local configuration without running actions
  /start REQUEST    Start a background request
  /task ID          Show live progress or collect its result/approval
  /tasks            List live task IDs and status (current process)
  /stop ID          Stop a background request after its current action
  /workflows [all|unfinished|state] [page]  Browse workflow records (50 per page)
  /prune-workflows [days] [keep]           Preview cleanup (default: 30 days, keep 100)
  /workflow ID      Review saved steps and pending arguments
  /resume ID        Explicitly resume saved steps (fresh approvals still required)
  /cancel ID        Cancel remaining saved steps
  /reset            Clear conversational context, keeping aliases and workflows
  /help             Show this help
  exit              Quit
Or type a natural-language request.
"""


async def local_command(agent: Agent, message: str) -> dict | None:
    command, _, argument = message.strip().partition(" ")
    argument = argument.strip()
    if command == "/tools":
        tools = agent.capabilities()
        if argument:
            tools = [
                tool
                for tool in tools
                if argument.casefold() in f"{tool['name']} {tool['description']}".casefold()
            ]
        print(json.dumps({"tools": tools}, indent=2))
    elif command == "/tasks":
        print(json.dumps(agent.list_tasks(), indent=2))
    elif command == "/start" and argument:
        print(json.dumps(agent.submit(argument), indent=2))
    elif command == "/task" and argument:
        progress = agent.task_progress(argument)
        if progress["result"]:
            return progress["result"]
        print(json.dumps(progress, indent=2))
    elif command == "/stop" and argument:
        print(json.dumps(agent.stop_task(argument), indent=2))
    elif command == "/help":
        print(HELP)
    elif command == "/workflows":
        parts = argument.split()
        if len(parts) > 2:
            raise ValueError("Usage: /workflows [all|unfinished|state] [page]")
        status = parts[0] if parts and parts[0] != "all" else None
        page = int(parts[1]) if len(parts) == 2 else 1
        query = WorkflowQuery(status=status, offset=(page - 1) * 50, limit=50)
        print(json.dumps(agent.workflow_page(query), indent=2))
    elif command == "/prune-workflows":
        parts = argument.split()
        if len(parts) > 2:
            raise ValueError("Usage: /prune-workflows [older-than-days] [keep-recent]")
        policy = RetentionPolicy(
            older_than_days=int(parts[0]) if parts else 30,
            keep_recent=int(parts[1]) if len(parts) == 2 else 100,
        )
        return await agent.preview_workflow_cleanup(policy)
    elif command == "/workflow" and argument:
        print(json.dumps(agent.get_workflow(argument), indent=2))
    elif command == "/resume" and argument:
        return await agent.resume_workflow(argument)
    elif command == "/cancel" and argument:
        return await agent.cancel_workflow(argument)
    elif command == "/reset":
        agent.reset_conversation()
        print("Conversation cleared.")
    else:
        print("Unknown or incomplete command. Type /help.")
    return None


async def present_result(agent: Agent, result: dict) -> dict:
    """One typed approval flow for text and voice; never approve from spoken input."""
    while result["status"] == "confirmation_required":
        confirmation = result["confirmation"]
        print(result["message"])
        print(json.dumps(confirmation["arguments"], indent=2))
        try:
            answer = await read_input("Approve this action? [y/N] ")
        except EOFError:
            answer = "n"
        result = await agent.confirm(confirmation["token"], answer.lower() == "y")
    print(result["message"])
    for step in result["steps"]:
        detail = step.get("result", step.get("error", "Previously recorded result"))
        print(
            f"{'✓' if step['success'] else '✗'} {step['tool']}: "
            f"{json.dumps(detail, ensure_ascii=False)}"
        )
    return result


def voice_notice(settings: Settings) -> None:
    destination = (
        "local Whisper" if settings.voice_stt_provider == "local" else "OpenAI (audio upload)"
    )
    print(f"Speech transcription: {destination}. Transcript model: {settings.llm_provider}.")
    print("Responses are spoken aloud. Approvals require typing in this terminal.")


async def voice_once(settings: Settings) -> None:
    from app.voice import VoiceService

    agent = build_agent(settings)
    service = None
    try:
        service = VoiceService(
            agent, settings, on_result=lambda result: present_result(agent, result)
        )
        voice_notice(settings)
        print(f"Recording one command for {settings.voice_record_seconds:g} seconds…")
        result = await service.listen_once()
        if "result" not in result:
            print(result["message"])
        if result.get("speech_error"):
            print(result["speech_error"])
    finally:
        try:
            if service:
                await service.close()
        finally:
            await agent.close()


async def voice_background(settings: Settings) -> None:
    from app.voice import VoiceService

    agent = build_agent(settings)
    service = None
    try:
        service = VoiceService(
            agent, settings, on_result=lambda result: present_result(agent, result)
        )
        voice_notice(settings)
        print("Starting explicitly configured local wake-word listening. Ctrl-C stops it.")
        await service.run_background()
    finally:
        try:
            if service:
                await service.close()
        finally:
            await agent.close()


async def cli(settings: Settings, verbose: bool = False):
    console_settings = settings if verbose else settings.model_copy(update={"log_level": "WARNING"})
    agent = build_agent(console_settings)
    print("Bridge\nType /help for commands or exit to quit.")
    if settings.llm_provider == "ollama":
        print(f"Provider: local Ollama ({settings.local_llm_model}); no OpenAI fallback.")
    else:
        print(
            f"Provider: OpenAI ({settings.openai_model}); messages are sent remotely. "
            f"Tool-result sharing: {settings.remote_tool_results}."
        )
    unfinished = agent.workflow_page(WorkflowQuery(status="unfinished", limit=1))["total"]
    if unfinished:
        print(f"{unfinished} unfinished workflow(s). Use /workflows unfinished to review them.")
    try:
        while True:
            try:
                message = await read_input("\n> ")
            except EOFError:
                break
            if message.strip().lower() in {"exit", "quit"}:
                break
            if not message.strip():
                continue
            try:
                if message.strip() == "/doctor":
                    print(json.dumps(collect_diagnostics(settings), indent=2))
                    continue
                if message.lstrip().startswith("/"):
                    result = await local_command(agent, message)
                    if result is None:
                        continue
                else:
                    result = await agent.message(message)
                await present_result(agent, result)
            except ValueError as exc:
                print(str(exc))
    finally:
        await agent.close()


def main():
    parser = argparse.ArgumentParser(description="Bridge desktop agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--doctor", action="store_true", help="Check configuration without starting the agent"
    )
    mode.add_argument("--api", action="store_true", help="Run the localhost API")
    mode.add_argument("--ui", action="store_true", help="Serve the local dashboard and API")
    mode.add_argument(
        "--menubar", action="store_true", help="Run the native macOS menu-bar launcher"
    )
    mode.add_argument(
        "--voice-once", action="store_true", help="Record one spoken command and run it"
    )
    mode.add_argument(
        "--voice-service",
        action="store_true",
        help="Run explicitly enabled wake-word/background voice mode",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show structured execution logs in CLI"
    )
    args = parser.parse_args()
    settings = Settings()
    if args.doctor:
        print(json.dumps(collect_diagnostics(settings), indent=2))
    elif args.voice_once:
        try:
            asyncio.run(voice_once(settings))
        except (KeyboardInterrupt, RuntimeError, ValueError) as exc:
            if str(exc):
                print(str(exc))
    elif args.voice_service:
        try:
            asyncio.run(voice_background(settings))
        except KeyboardInterrupt:
            print("\nVoice service stopped.")
        except (RuntimeError, ValueError) as exc:
            print(str(exc))
    elif args.menubar:
        from app.desktop.menubar import run_menubar

        try:
            run_menubar(settings)
        except RuntimeError as exc:
            print(str(exc))
    elif args.api or args.ui:
        import uvicorn

        from app.api.server import create_app

        if args.ui:
            print(
                "Desktop Agent dashboard: http://127.0.0.1:8000\n"
                "Connect using API_TOKEN from your .env file."
            )
        uvicorn.run(
            create_app(settings, enable_ui=args.ui), host="127.0.0.1", port=8000, access_log=False
        )
    else:
        try:
            asyncio.run(cli(settings, args.verbose))
        except KeyboardInterrupt:
            print("\nGoodbye.")
        except RuntimeError as exc:
            print(str(exc))


if __name__ == "__main__":
    main()
