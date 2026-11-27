"""CI entry point, inside a credential-free bubblewrap namespace; no inference request."""
import asyncio
import sys

from opencognitiveos.providers.acp import initialize
from opencognitiveos.providers.pi_rpc import get_state


async def main():
    adapter = sys.argv[1]
    process = await asyncio.create_subprocess_exec(*sys.argv[2:], stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, limit=65536)
    try:
        if adapter == "pi":
            result = await get_state(process.stdout, process.stdin, timeout=30)
            print("Pi RPC get_state returned fields:", sorted(result), flush=True)
        else:
            result = await initialize(process.stdout, process.stdin, timeout=30)
            print(adapter, "ACP version", result["protocolVersion"], flush=True)
    finally:
        process.stdin.close()
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


asyncio.run(main())
