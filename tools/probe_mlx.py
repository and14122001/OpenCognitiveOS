"""Native MLX forward pass with deterministic, synthetic tiny Llama weights."""
from importlib.metadata import version
import platform

import mlx.core as mx
from mlx_lm.models.llama import Model, ModelArgs

mx.set_default_device(mx.cpu)
mx.random.seed(41)
model = Model(ModelArgs(model_type="llama", hidden_size=32, num_hidden_layers=1, intermediate_size=64,
                        num_attention_heads=4, num_key_value_heads=2, rms_norm_eps=1e-5, vocab_size=32))
model.eval()
tokens = mx.array([[1, 2, 3]])
logits = model(tokens)
mx.eval(logits)
assert logits.shape == (1, 3, 32)
assert mx.all(mx.isfinite(logits)).item()
next_id = int(mx.argmax(logits[0, -1]).item())
assert 0 <= next_id < 32
print("architecture", platform.machine(), "device", mx.default_device(),
      "mlx", version("mlx"), "mlx-lm", version("mlx-lm"), "next token", next_id)
