"""Start the pinned public CPU fixture inside the same network namespace as a supervised agent."""
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen


def start():
    proc = subprocess.Popen(["/model-runtime/llama-server", "-m", "/model-fixture.gguf",
        "--alias", "ocos-fixture", "--host", "127.0.0.1", "--port", "18080", "-t", "2",
        "-c", "4096", "-np", "1", "--no-webui"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            if proc.poll() is not None:
                raise RuntimeError("Local fixture server exited during startup")
            try:
                with urlopen("http://127.0.0.1:18080/health", timeout=1) as response:
                    if json.load(response).get("status") == "ok":
                        return proc
            except OSError:
                pass
            time.sleep(.1)
        raise RuntimeError("Local fixture model failed readiness")
    except BaseException:
        proc.terminate()
        proc.wait(timeout=5)
        raise


def configure_pi():
    path = Path("/tmp/.pi/agent")
    path.mkdir(parents=True, exist_ok=True)
    settings = {"providers": {"local-fixture": {"baseUrl":"http://127.0.0.1:18080/v1",
        "api":"openai-completions", "apiKey":"public-fixture", "models":[{"id":"ocos-fixture",
        "contextWindow":4096, "maxTokens":16}]}}}
    (path/"models.json").write_text(json.dumps(settings))
