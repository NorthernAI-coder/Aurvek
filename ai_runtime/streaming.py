from ai_runtime.dependencies import *

SSE_KEEPALIVE_INTERVAL_SECONDS = 15
async def _stream_with_sse_keepalives(source, interval: int = SSE_KEEPALIVE_INTERVAL_SECONDS):
    """Yield SSE comments while waiting for slow provider chunks.

    Cloudflare can time out long-running proxied requests that do not emit
    response bytes. SSE comments are ignored by the chat renderer but keep the
    HTTP stream active while uploads, PDF handling, or provider thinking take
    longer than usual.
    """
    iterator = source.__aiter__()
    pending = asyncio.create_task(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield ": keep-alive\n\n"
                continue

            try:
                chunk = pending.result()
            except StopAsyncIteration:
                break

            yield chunk
            pending = asyncio.create_task(iterator.__anext__())
    finally:
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with suppress(Exception):
                await aclose()
