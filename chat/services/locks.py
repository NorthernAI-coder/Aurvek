import asyncio
from contextlib import asynccontextmanager


conversation_locks = {}
conversation_locks_guard = asyncio.Lock()


@asynccontextmanager
async def conversation_write_lock(conversation_id: int):
    async with conversation_locks_guard:
        lock = conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            conversation_locks[conversation_id] = lock
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
