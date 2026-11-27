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


async def prompt_text(reader, writer, message, *, timeout=30):
    """Finish one supervised Pi RPC turn. Command acknowledgement alone is insufficient."""
    if not isinstance(message, str) or not 0 < len(message.encode()) <= 4096:
        raise ValueError("Invalid prompt length")
    rid = str(uuid.uuid4())

    async def exchange():
        writer.write(json.dumps({"type": "prompt", "id": rid, "message": message}).encode() + b"\n")
        await writer.drain()
        accepted = False
        answer = None
        for _ in range(256):
            raw = await reader.readuntil(b"\n")
            if len(raw) > 65536:
                raise ValueError("Pi event exceeds limit")
            event = json.loads(raw)
            kind = event.get("type")
            if kind == "response":
                if (event.get("id") != rid or event.get("command") != "prompt" or event.get("success") is not True
                        or event.get("data", {}).get("disposition") != "started"):
                    raise ValueError("Pi did not start the requested turn")
                accepted = True
            elif kind == "message_end":
                msg = event.get("message", {})
                if msg.get("role") == "assistant":
                    if msg.get("stopReason") not in ("stop", "length") or msg.get("errorMessage"):
                        raise ValueError("Pi assistant turn failed: " + str(msg.get("stopReason")))
                    answer = "".join(x["text"] for x in msg["content"] if x.get("type") == "text")
            elif kind in ("tool_execution_start", "extension_ui_request"):
                raise ValueError("Text-only turn requested an unauthorized operation")
            elif kind == "agent_settled":
                if accepted and answer and len(answer.encode()) <= 16384:
                    return answer
                raise ValueError("Pi settled without a successful assistant message")
        raise ValueError("Pi event budget exhausted")

    return await asyncio.wait_for(exchange(), timeout)
