"""Acknowledged Redis delivery for provider-generated media results."""

from __future__ import annotations

import asyncio
import time

import orjson


async def publish_result_with_ack(
    redis_client,
    *,
    channel_name: str,
    payload: dict,
    reservation_id: str,
    timeout_seconds: float = 60.0,
) -> None:
    """Publish a result and wait until its database persistence is confirmed."""
    ack_channel = f"{channel_name}:delivery-ack"
    ack_token = str(reservation_id)
    outbound = dict(payload)
    outbound["_delivery_ack"] = {
        "channel": ack_channel,
        "token": ack_token,
        "reservation_id": ack_token,
    }

    async with redis_client.pubsub() as ack_subscriber:
        await ack_subscriber.subscribe(ack_channel)
        subscribers = await redis_client.publish(
            channel_name,
            orjson.dumps(outbound).decode(),
        )
        if int(subscribers or 0) < 1:
            raise RuntimeError("Provider result could not be delivered")

        # The consumer can now finish its generator, persist the message and
        # publish the acknowledgement while this worker remains subscribed.
        await redis_client.publish(channel_name, "END")
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            message = await ack_subscriber.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message:
                value = message.get("data")
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                if str(value) == ack_token:
                    return
            await asyncio.sleep(0)

    raise RuntimeError("Provider result persistence was not acknowledged")
