"""Exercise the pinned official Gemini SDK against a synthetic loopback service."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading

from google import genai
from google.genai import types

from opencognitiveos.contracts.models import DataClass, TaskSpec, TaskStatus
from opencognitiveos.errors import PolicyDeniedError
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.providers.cloud_quota import CloudQuotaLedger
from opencognitiveos.providers.google_cloud import GeminiDirectAdapter
from opencognitiveos.storage.sqlite_store import SQLiteStore


class Server(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, payload))
        raw = json.dumps({"candidates":[{"content":{"role":"model","parts":[{"text":"fixture reply"}]},
                          "finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,
                          "candidatesTokenCount":2,"totalTokenCount":4}}).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    with tempfile.TemporaryDirectory() as tmp, HTTPServer(("127.0.0.1",0), Server) as httpd:
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        try:
            store = SQLiteStore(Path(tmp)/"db")
            store.migrate()
            auth = ExecutionAuthority(store)
            control = object()
            ledger = CloudQuotaLedger(auth, controller_capability=control)
            task = TaskSpec("public-ci","r","sdk","Public fixture","fixture",cloud_approved=True,
                      data_class=DataClass.PUBLIC,allowed_providers=("gemini-direct:gemini-fixture",))
            rec,_ = auth.submit_task(task)
            ledger.grant(capability=control,task=task.unique_key,provider=task.allowed_providers[0],
                         pool="local",pool_tokens=12,task_tokens=12,expires="2099-01-01T00:00:00Z")
            for state in (TaskStatus.PLANNED, TaskStatus.READY):
                rec = auth.transition_task(task.unique_key, state, expected_revision=rec.revision,
                            actor_role="controller", controller_capability=auth._controller_capability)
            lease = auth.acquire_lease(task.unique_key,"controller")
            fence = dict(worker_id="controller",execution_id=lease.execution_id,epoch=lease.fencing_epoch)
            auth.transition_task(task.unique_key,TaskStatus.RUNNING,expected_revision=rec.revision,**fence)
            opts = types.HttpOptions(base_url=f"http://127.0.0.1:{httpd.server_port}",api_version="v1beta",
                                     client_args={"trust_env":False}, async_client_args={"trust_env":False})
            with genai.Client(api_key="public-local-fixture", http_options=opts) as client:
                quote = {"max_cost_microusd":None,"max_input_tokens":4}
                adapter = GeminiDirectAdapter(client=client, ledger=ledger, capability=control, model="gemini-fixture",
                                             quote=lambda *_:quote)
                call = dict(request_id="1",max_output_tokens=4,**fence)
                try:
                    adapter.generate(task.unique_key, **call)
                except PolicyDeniedError:
                    pass
                else:
                    raise AssertionError("Unknown cost was accepted")
                assert not Server.requests
                quote["max_cost_microusd"] = 0
                assert adapter.generate(task.unique_key, **call) == "fixture reply"
                assert len(Server.requests) == 1
                path, request = Server.requests[0]
                assert path == "/v1beta/models/gemini-fixture:generateContent"
                assert request["contents"][0]["parts"][0]["text"] == task.goal
                assert request["generationConfig"]["maxOutputTokens"] == 4
                assert not request.get("tools")
            with store.open_connection() as conn:
                assert conn.execute("SELECT actual FROM cloud_usage").fetchone()[0] == 4
            print("Official Google Gen AI SDK: unknown-cost transport count 0; one loopback request; settled 4 tokens.")
        finally:
            httpd.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
