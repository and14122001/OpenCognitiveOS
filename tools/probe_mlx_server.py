"""Exercise local HTTP inference with the real MLX-LM server and small synthetic weights."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import mlx.core as mx
from mlx.utils import tree_flatten
from mlx_lm.models.llama import Model, ModelArgs
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from transformers import PreTrainedTokenizerFast

from opencognitiveos.contracts.models import TaskSpec, TaskStatus
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.kernel.inference import InferenceCoordinator
from opencognitiveos.providers.mlx_http import LocalModelServer
from opencognitiveos.storage.sqlite_store import SQLiteStore


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    model_dir = root / "model"
    model_dir.mkdir()
    mx.set_default_device(mx.cpu)
    mx.random.seed(41)
    args = ModelArgs(model_type="llama", hidden_size=32, num_hidden_layers=1, intermediate_size=64,
                     num_attention_heads=4, num_key_value_heads=2, rms_norm_eps=1e-5, vocab_size=32)
    model = Model(args)
    config = {k: v for k, v in asdict(args).items() if v is not None}
    config.update(max_position_embeddings=128, bos_token_id=1, eos_token_id=2)
    (model_dir / "config.json").write_text(json.dumps(config))
    mx.save_safetensors(str(model_dir / "model.safetensors"), dict(tree_flatten(model.parameters())), metadata={"format":"mlx"})
    vocab = {"<unk>": 0, "<s>": 1, "</s>": 2, **{str(n): n for n in range(3, 32)}}
    raw_tokenizer = Tokenizer(WordLevel(vocab, unk_token="<unk>"))
    raw_tokenizer.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=raw_tokenizer, unk_token="<unk>", bos_token="<s>", eos_token="</s>")
    tokenizer.chat_template = "{{ bos_token }}{% for message in messages %}{{ message['content'] }} {% endfor %}"
    tokenizer.save_pretrained(str(model_dir))
    cache = root / "hub"
    cache.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-c",
        "import mlx.core as mx; mx.set_default_device(mx.cpu); from mlx_lm.server import main; main()",
        "--model", str(model_dir), "--host", "127.0.0.1", "--port", str(port)],
        env={**os.environ, "HF_HUB_OFFLINE":"1", "HF_HUB_CACHE":str(cache)})
    try:
        server = LocalModelServer("127.0.0.1", port)
        for _ in range(60):
            if proc.poll() is not None:
                raise RuntimeError("MLX server exited during startup")
            try:
                if str(model_dir) in server.models():
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("MLX model unavailable")
        store = SQLiteStore(root / "tasks.db")
        store.migrate()
        auth = ExecutionAuthority(store)
        task = TaskSpec("fixture", "local", "infer", "3 4", "fixture", allowed_providers=("mlx-local",))
        rec, _ = auth.submit_task(task)
        control = object()
        coordinator = InferenceCoordinator(auth, controller_capability=control)
        coordinator.allocate(task.unique_key, str(model_dir), "fixture-v1", "fixture-policy", 32, capability=control)
        for status in (TaskStatus.PLANNED, TaskStatus.READY):
            rec = auth.transition_task(rec.unique_key, status, expected_revision=rec.revision, actor_role="controller", controller_capability=auth._controller_capability)
        lease = auth.acquire_lease(task.unique_key, "local")
        fence = dict(worker_id="local", execution_id=lease.execution_id, epoch=lease.fencing_epoch)
        auth.transition_task(task.unique_key, TaskStatus.RUNNING, expected_revision=rec.revision, **fence)
        out = server.complete(coordinator, task.unique_key, supervisor=control, request_id="1",
                              prompt=task.goal, input_token_limit=16, output_token_limit=2, **fence)
        with store.open_connection() as conn:
            row = conn.execute("SELECT state,used FROM inference_calls").fetchone()
        assert row["state"] == "CLOSED" and 0 < row["used"] <= 18
        print("MLX HTTP completion; settled tokens", row["used"], "output characters", len(out))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
