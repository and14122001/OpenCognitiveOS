"""CI entry point, inside a credential-free bubblewrap namespace; no inference request."""
import asyncio
import sys

from opencognitiveos.providers.acp import initialize, open_session
from opencognitiveos.providers.pi_rpc import get_state, prompt_text


async def main():
    adapter = sys.argv[1]
    model = None
    if adapter == "pi":
        from local_agent_model import configure_pi, start
        model = start()
        configure_pi()
    process = await asyncio.create_subprocess_exec(*sys.argv[2:], stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, limit=65536)
    try:
        if adapter == "pi":
            result = await get_state(process.stdout, process.stdin, timeout=30)
            print("Pi RPC get_state returned fields:", sorted(result), flush=True)
            text = await prompt_text(process.stdout, process.stdin, "What is 2 + 2?", timeout=45)
            print("Pi real local model turn settled, text characters:", len(text), flush=True)
        else:
            result = await initialize(process.stdout, process.stdin, timeout=30)
            print(adapter, "ACP version", result["protocolVersion"], flush=True)
            await open_session(process.stdout, process.stdin, cwd="/tmp", timeout=30)
            print(adapter, "credential-free session/new succeeded", flush=True)
    finally:
        if model is not None:
            model.terminate()
            model.wait(timeout=5)
        process.stdin.close()
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


asyncio.run(main())
