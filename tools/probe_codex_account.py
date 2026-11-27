"""CI runs this probe in an empty, network-isolated public fixture home."""
import asyncio
import sys
from opencognitiveos.providers.codex_account import read_account


async def main():
    process = await asyncio.create_subprocess_exec(sys.argv[1], "app-server",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=65536)
    try:
        state = await read_account(process.stdout, process.stdin)
        assert not state["authenticated"] and not state["dispatch_authorized"]
        print("Official Codex app-server: account/read returned signed out. Model dispatch denied.")
    finally:
        process.stdin.close()
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
