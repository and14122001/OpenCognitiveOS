"""ACP initialization on an already supervised asyncio stdio transport."""

import asyncio
import json
import uuid


class ACPProtocolError(ValueError):
    """The peer did not negotiate the supported ACP protocol."""


async def initialize(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     *, timeout: float = 5.0) -> dict:
    """Negotiate ACP v1. Advertise no filesystem, tool, or terminal capabilities."""
    request_id = str(uuid.uuid4())
    request = {"jsonrpc": "2.0", "id": request_id, "method": "initialize",
               "params": {"protocolVersion": 1, "clientCapabilities": {}}}

    async def exchange():
        writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        raw = await reader.readuntil(b"\n")
        if len(raw) > 65536:
            raise ACPProtocolError("ACP initialization response too large.")
        reply = json.loads(raw)
        if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
                or reply.get("id") != request_id or "method" in reply or "error" in reply):
            raise ACPProtocolError("Unexpected ACP initialization response.")
        result = reply.get("result")
        if (not isinstance(result, dict) or type(result.get("protocolVersion")) is not int
                or result["protocolVersion"] != 1 or not isinstance(result.get("agentCapabilities"), dict)):
            raise ACPProtocolError("Unsupported ACP protocol or capabilities.")
        return result

    return await asyncio.wait_for(exchange(), timeout=timeout)
