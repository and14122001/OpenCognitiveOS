"""Read-only account preflight on a supervised Codex app-server. No credential refresh or model turn."""
import asyncio
import json


async def read_account(reader, writer, *, timeout=10):
    async def send(packet):
        writer.write(json.dumps(packet).encode() + b"\n")
        await writer.drain()

    async def request(number, method, params):
        await send({"id":number, "method":method, "params":params})
        for _ in range(16):
            frame = await reader.readuntil(b"\n")
            if len(frame) > 65536:
                raise ValueError("Codex frame exceeds limit")
            message = json.loads(frame)
            if message.get("id") == number and isinstance(message.get("result"), dict) and "error" not in message:
                return message["result"]
            if "id" in message or not isinstance(message.get("method"), str):
                raise ValueError("Unexpected Codex response")
        raise ValueError("Codex notification quota exceeded")

    async def exchange():
        await request(1, "initialize", {"clientInfo":{"name":"opencognitiveos","version":"0.1.6"}})
        await send({"method":"initialized", "params":{}})
        result = await request(2, "account/read", {"refreshToken":False})
        if "account" not in result or type(result.get("requiresOpenaiAuth")) is not bool:
            raise ValueError("Unknown Codex account shape")
        account = result["account"]
        if account is not None and not isinstance(account, dict):
            raise ValueError("Invalid Codex account")
        # Presence of an account/plan does not supply a task-specific zero-cost entitlement.
        return {"authenticated": account is not None, "auth_required":result["requiresOpenaiAuth"],
                "dispatch_authorized":False}
    return await asyncio.wait_for(exchange(), timeout)
