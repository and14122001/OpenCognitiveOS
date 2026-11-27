"""CI entry point, inside a credential-free bubblewrap namespace with a pinned local CPU model."""
import asyncio
import sys

from opencognitiveos.providers.acp import initialize, open_session, prompt_text as acp_prompt
from opencognitiveos.providers.pi_rpc import get_state, prompt_text


async def main():
    adapter = sys.argv[1]
    from local_agent_model import configure_acp, configure_pi, generated_tokens, start
    model = start(adapter)
    if adapter == "pi":
        configure_pi()
    else:
        configure_acp(adapter)
    before = generated_tokens()
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
            sid = await open_session(process.stdout, process.stdin, cwd="/tmp", timeout=30)
            print(adapter, "credential-free session/new succeeded", flush=True)
            answer = await acp_prompt(process.stdout, process.stdin, sid, "What is 2 + 2?", timeout=110)
            print(adapter, "completed text characters:", len(answer), flush=True)
        generated = generated_tokens() - before
        if generated <= 0:
            raise RuntimeError("No generation on the isolated local server")
        print(adapter, "verified local model generated tokens:", generated, flush=True)
    except Exception as exc:
        from local_agent_model import print_fixture_diagnostics
        print_fixture_diagnostics()
        remote = getattr(exc, "remote_error", None)
        if remote is not None:
            print("Public fixture ACP diagnostic:", str(remote)[:1200], flush=True)
        print("Isolated model generated tokens before failure:", generated_tokens() - before, flush=True)
        raise
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
