"""Controller-only loopback adapter for OpenAI-compatible MLX/oMLX servers."""
from __future__ import annotations

import http.client
import ipaddress
import json
from contextlib import closing

from opencognitiveos.errors import PolicyDeniedError


class LocalModelServer:
    def __init__(self, host, port, *, timeout=30):
        if not ipaddress.ip_address(host).is_loopback or type(port) is not int or not 0 < port < 65536:
            raise PolicyDeniedError("Local inference requires a numeric loopback endpoint.")
        self.host, self.port, self.timeout = host, port, timeout

    def _request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        with closing(http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)) as conn:
            conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                raise PolicyDeniedError(f"Local model server returned HTTP {response.status}.")
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise PolicyDeniedError("Model response exceeds limit.")
            return json.loads(raw)

    def models(self):
        data = self._request("GET", "/v1/models")["data"]
        return tuple(item["id"] for item in data)

    def complete(self, coordinator, task, *, supervisor, worker_id, execution_id, epoch,
                 request_id, prompt, input_token_limit, output_token_limit):
        if (not isinstance(prompt, str) or not 0 < len(prompt.encode()) <= 65536
                or type(input_token_limit) is not int or input_token_limit <= 0
                or type(output_token_limit) is not int or output_token_limit <= 0):
            raise PolicyDeniedError("Bounded prompt and token limits required.")
        # Only the trusted serving supervisor may dispatch or settle. An interrupted HTTP
        # exchange leaves the reservation active until it confirms remote inference stopped.
        coordinator.authorize_supervisor(supervisor)
        rid, snapshot = coordinator.begin(task, worker_id=worker_id, execution_id=execution_id, epoch=epoch,
                                         request_id=request_id, max_tokens=input_token_limit + output_token_limit)
        model = snapshot["model_id"]
        if model not in self.models():
            coordinator.settle(rid, 0, capability=supervisor)
            raise PolicyDeniedError("Allocated model missing from local service.")
        reply = self._request("POST", "/v1/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": output_token_limit, "stream": False})
        usage = reply["usage"]
        used = usage["total_tokens"]
        if (reply.get("model") != model or type(used) is not int
                or type(usage.get("prompt_tokens")) is not int or type(usage.get("completion_tokens")) is not int
                or not 0 <= usage["prompt_tokens"] <= input_token_limit
                or not 0 <= usage["completion_tokens"] <= output_token_limit
                or used != usage["prompt_tokens"] + usage["completion_tokens"]):
            raise PolicyDeniedError("Unknown model or inference usage; reservation requires reconciliation.")
        text = reply["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise PolicyDeniedError("Malformed proposal.")
        coordinator.settle(rid, used, capability=supervisor)
        return text
