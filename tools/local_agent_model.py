"""Start the pinned public CPU fixture inside the same network namespace as a supervised agent."""
import json
import os
import re
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen


def start(adapter):
    with Path("/tmp/fixture-model.log").open("wb") as log:
        proc = subprocess.Popen(["/model-runtime/llama-server", "-m", "/model-fixture.gguf",
        "--alias", "ocos-fixture", "--host", "127.0.0.1", "--port", "18080", "-t", "2",
        "-c", "65536" if adapter == "hermes" else "8192", "-np", "1", "--metrics", "--reasoning", "off", "-n", "48", "--no-webui"], stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(200):
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

def configure_acp(adapter):
    base = "http://127.0.0.1:18080/v1"
    if adapter == "opencode":
        settings = {"model":"fixture/ocos-fixture","small_model":"fixture/ocos-fixture",
            "permission":"deny", "enabled_providers":["fixture"], "provider":{"fixture":{
            "npm":"@ai-sdk/openai-compatible","name":"Local fixture","options":{"baseURL":base,
            "apiKey":"public-fixture"},"models":{"ocos-fixture":{"name":"Fixture",
            "limit":{"context":8192,"output":32}}}}}}
        os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(settings)
        os.environ["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
    else:
        path = Path("/tmp/hermes-home")
        path.mkdir()
        os.environ["HERMES_HOME"] = str(path)
        settings = {"model":{"default":"ocos-fixture","provider":"custom:fixture","base_url":base},
            "providers":{"fixture":{"api":base,"api_key":"public-fixture","default_model":"ocos-fixture",
            "transport":"chat_completions","context_length":65536,"extra_body":{"max_tokens":32}}},
            "platform_toolsets":{"acp":[]}, "agent":{"max_turns":2, "reasoning_effort":"none"}}
        # JSON is a YAML 1.2 subset.
        (path/"config.yaml").write_text(json.dumps(settings))


def generated_tokens():
    with urlopen("http://127.0.0.1:18080/metrics", timeout=2) as response:
        metrics = response.read(1048576).decode()
    match = re.search(r"^llamacpp:tokens_predicted_total\s+([0-9.eE+-]+)$", metrics, re.M)
    if not match:
        raise ValueError("No local model generation metric")
    return int(float(match[1]))

def print_fixture_diagnostics():
    # This namespace contains a new synthetic home, a public model and no credentials.
    paths = [Path("/tmp/fixture-model.log"), *Path("/tmp/hermes-home/logs").glob("*.log")]
    for path in paths:
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 32768))
            lines = stream.read(32768).decode("utf-8", "replace").splitlines()
        selected = [line for line in lines if re.search(r"error|exception|fail|denied|400|invalid", line, re.I)]
        for line in selected[-8:]:
            print("Isolated fixture log", path.name, line[:320], flush=True)
