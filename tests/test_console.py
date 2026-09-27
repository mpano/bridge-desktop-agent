import asyncio
import os

import pytest

from app.console import read_input


async def test_input_cancellation_removes_reader_and_allows_next_read():
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r", encoding="utf-8") as stream:
        try:
            task = asyncio.create_task(read_input("", stream))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            os.write(write_fd, "héllo\nnext\n".encode())
            assert await asyncio.wait_for(read_input("", stream), 2) == "héllo"
            assert await asyncio.wait_for(read_input("", stream), 2) == "next"
        finally:
            os.close(write_fd)
        with pytest.raises(EOFError):
            await asyncio.wait_for(read_input("", stream), 2)
