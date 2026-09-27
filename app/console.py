"""Cancellable terminal input without a blocked executor thread on shutdown."""

import asyncio
import os
import stat
import sys
from typing import TextIO


async def read_input(prompt: str, stream: TextIO | None = None) -> str:
    stream = stream or sys.stdin
    print(prompt, end="", flush=True)
    fd = stream.fileno()
    if stat.S_ISREG(os.fstat(fd).st_mode):
        line = stream.readline()
        if not line:
            raise EOFError
        return line.rstrip("\r\n")
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    data = bytearray()

    def ready() -> None:
        if result.done():
            return
        try:
            value = os.read(fd, 1)
            if value in (b"", b"\n"):
                if not value and not data:
                    result.set_exception(EOFError())
                else:
                    result.set_result(data.decode(stream.encoding or "utf-8").rstrip("\r"))
            else:
                data.extend(value)
        except Exception as exc:
            result.set_exception(exc)

    try:
        loop.add_reader(fd, ready)
    except (OSError, NotImplementedError) as exc:
        raise RuntimeError("Interactive input requires a supported terminal or pipe.") from exc
    try:
        return await result
    finally:
        loop.remove_reader(fd)
