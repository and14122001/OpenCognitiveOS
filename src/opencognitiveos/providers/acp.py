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


async def open_session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       *, cwd: str, timeout: float = 15.0) -> str:
    """Create a credential-free session in a supervisor-provided absolute sandbox directory."""
    from pathlib import PurePosixPath
    path = PurePosixPath(cwd)
    if not path.is_absolute() or ".." in path.parts:
        raise ACPProtocolError("Session requires an absolute sandbox directory.")
    request_id = str(uuid.uuid4())
    request = {"jsonrpc": "2.0", "id": request_id, "method": "session/new",
               "params": {"cwd": cwd, "mcpServers": []}}

    async def exchange():
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        # Agents can announce scoped updates before returning the session handle.
        sessions = set()
        for _ in range(32):
            raw = await reader.readuntil(b"\n")
            if len(raw) > 65536:
                raise ACPProtocolError("Session frame too large.")
            reply = json.loads(raw)
            if not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0":
                raise ACPProtocolError("Invalid session frame.")
            if reply.get("method") == "session/update" and "id" not in reply:
                params = reply.get("params")
                if not isinstance(params, dict) or not isinstance(params.get("sessionId"), str):
                    raise ACPProtocolError("Unscoped notification.")
                sessions.add(params["sessionId"])
                continue
            if reply.get("id") != request_id or "method" in reply or "error" in reply:
                raise ACPProtocolError("Session unavailable without additional capabilities.")
            result = reply.get("result")
            sid = result.get("sessionId") if isinstance(result, dict) else None
            if not isinstance(sid, str) or not 1 <= len(sid) <= 512 or sessions - {sid}:
                raise ACPProtocolError("Invalid session handle.")
            return sid
        raise ACPProtocolError("Session notification quota exhausted.")

    return await asyncio.wait_for(exchange(), timeout=timeout)
