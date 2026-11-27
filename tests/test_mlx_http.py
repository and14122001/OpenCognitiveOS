import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock

from opencognitiveos.errors import PolicyDeniedError
from opencognitiveos.providers.mlx_http import LocalModelServer


class MLXProtocolTests(unittest.TestCase):
    def test_loopback_request_and_usage(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, data):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_GET(self):
                self.answer({"data": [{"id": "fixture"}]})

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.answer({"model": request["model"], "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                             "total_tokens": 4}, "choices": [{"message": {"content": "ok"}}]})

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = LocalModelServer(*server.server_address)
                supervisor = object()
                coordinator = Mock()
                coordinator.begin.return_value = ("id", {"model_id": "fixture"})
                text = client.complete(coordinator, "task", supervisor=supervisor, worker_id="w", execution_id="e",
                        epoch=1, request_id="1", prompt="test", input_token_limit=5, output_token_limit=2)
                self.assertEqual(text, "ok")
                coordinator.settle.assert_called_once_with("id", 4, capability=supervisor)
            finally:
                server.shutdown()
                thread.join()
        with self.assertRaises(PolicyDeniedError):
            LocalModelServer("192.0.2.1", 8080)
