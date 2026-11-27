"""Task-specific Gemini DirectSDK boundary. Transport is supplied by the trusted controller."""
from opencognitiveos.contracts.models import DataClass
from opencognitiveos.errors import PolicyDeniedError


class GeminiDirectAdapter:
    def __init__(self, *, client, ledger, capability, model, quote):
        self.client, self.ledger, self._capability = client, ledger, capability
        self.model = model
        self._quote = quote

    def generate(self, task_key, *, worker_id, execution_id, epoch, request_id, max_output_tokens):
        task = self.ledger.authority.get_task(task_key).spec
        provider = "gemini-direct:" + self.model
        if (provider not in task.allowed_providers or task.data_class != DataClass.PUBLIC or not task.cloud_approved
                or type(max_output_tokens) is not int or max_output_tokens <= 0):
            raise PolicyDeniedError("Task or model has no public-data cloud approval.")
        # The trusted quote is for this account, model, task, immutable goal and output cap.
        # A subscription or an installed SDK supplies no zero-cost guarantee.
        quoted = self._quote(task, self.model, max_output_tokens)
        if (not isinstance(quoted, dict) or type(quoted.get("max_input_tokens")) is not int
                or quoted["max_input_tokens"] <= 0):
            raise PolicyDeniedError("Missing trusted input-token bound.")
        reserved = self.ledger.reserve(capability=self._capability, task=task_key, provider=provider,
                    request_id=request_id, max_tokens=quoted["max_input_tokens"] + max_output_tokens,
                    guaranteed_cost_microusd=quoted.get("max_cost_microusd"), worker_id=worker_id,
                    execution_id=execution_id, epoch=epoch)
        if not reserved:
            raise PolicyDeniedError("Already dispatched or unresolved; reconcile this request.")
        # One transport attempt per reservation, with optional model tools disabled.
        reply = self.client.models.generate_content(model=self.model, contents=task.goal,
                    config={"max_output_tokens": max_output_tokens, "candidate_count": 1,
                            "http_options": {"retry_options": {"attempts": 1}, "timeout": 30000},
                            "automatic_function_calling": {"disable": True}})
        metadata = reply.usage_metadata
        used = metadata.total_token_count
        self.ledger.settle(capability=self._capability, task=task_key, request_id=request_id, actual_tokens=used)
        if not isinstance(reply.text, str):
            raise PolicyDeniedError("No textual proposal.")
        return reply.text
