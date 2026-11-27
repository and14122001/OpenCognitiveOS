import asyncio
import json
import unittest

from opencognitiveos.providers.acp import ACPProtocolError, initialize


class ACPHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, version):
        async def respond(reader, writer):
            try:
                req = json.loads(await reader.readline())
                self.assertEqual(req["params"]["clientCapabilities"], {})
                result = {"protocolVersion": version, "agentCapabilities": {}}
                writer.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode() + b"\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
            try:
                return await initialize(reader, writer)
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_v1_initializes(self):
        self.assertEqual((await self.exchange(1))["protocolVersion"], 1)

    async def test_unknown_version_rejected(self):
        with self.assertRaises(ACPProtocolError):
            await self.exchange(2)
