"""Read-only Pi RPC state negotiation on a supervised transport."""
import asyncio
import json
import uuid


async def get_state(reader, writer, *, timeout=10):
    request_id = str(uuid.uuid4())

    async def query():
        writer.write(json.dumps({"type": "get_state", "id": request_id}).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readuntil(b"\n"))
        if (response.get("type") != "response" or response.get("command") != "get_state"
                or response.get("id") != request_id or response.get("success") is not True
                or not isinstance(response.get("data"), dict)):
            raise ValueError("Pi returned an unexpected state response.")
        return response["data"]

    return await asyncio.wait_for(query(), timeout)
