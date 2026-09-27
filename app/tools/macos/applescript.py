import asyncio
import json
import sys

from app.security.permissions import operation_error


class NativeRunner:
    async def run(
        self,
        *argv: str,
        cwd: str | None = None,
        env: dict | None = None,
        input_text: str | None = None,
        strip_output: bool = True,
    ) -> str:
        if sys.platform != "darwin":
            raise RuntimeError("Native desktop tools require macOS.")
        input_bytes = input_text.encode("utf-8") if input_text is not None else None
        if input_bytes is not None and len(input_bytes) > 65536:
            raise ValueError("Operation input exceeded the safety limit.")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE
            if input_bytes is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=65536,
        )

        async def read(stream):
            output = bytearray()
            while chunk := await stream.read(4096):
                if len(output) + len(chunk) > 65536:
                    raise RuntimeError("Operation output exceeded the safety limit.")
                output.extend(chunk)
            return output.decode(errors="replace")

        try:
            async with asyncio.timeout(30):

                async def write():
                    if input_bytes is not None:
                        process.stdin.write(input_bytes)
                        await process.stdin.drain()
                        process.stdin.close()

                stdout, stderr, _ = await asyncio.gather(
                    read(process.stdout), read(process.stderr), write()
                )
                await process.wait()
            if process.returncode:
                raise RuntimeError(operation_error(stderr))
            return stdout.strip() if strip_output else stdout
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


class MacOSAppleScript:
    def __init__(self, runner: NativeRunner):
        self.runner = runner

    @staticmethod
    def literal(value: str) -> str:
        if any(ord(c) < 32 for c in value):
            raise ValueError("Control characters are not allowed")
        return json.dumps(value, ensure_ascii=False)

    async def run(self, script: str) -> str:
        return await self.runner.run("/usr/bin/osascript", "-e", script)

    async def application(self, name: str, action: str) -> str:
        if action not in {"activate", "quit", "running"}:
            raise ValueError("Unsupported application action")
        target = self.literal(name)
        if action == "running":
            return await self.run(f"application {target} is running")
        return await self.run(f"tell application {target} to {action}")
