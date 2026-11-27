"""Offline MockProvider and cloud-stub for PolicyGate verification."""

from __future__ import annotations

from typing import Any

from opencognitiveos.contracts.models import canonical_json_hash


class MockProvider:
    """Deterministic offline provider routed through PolicyGate."""

    name: str = "mock-local"
    is_cloud: bool = False

    def __init__(self) -> None:
        self.cancelled_runs: set[str] = set()

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "is_cloud": self.is_cloud,
            "offline": True,
            "cost_per_call_usd": 0.0,
            "tools": ["mock.echo", "mock.write_note"],
        }

    def infer(self, request: dict[str, Any]) -> dict[str, Any]:
        run_id = str(request.get("run_id", "mock-run-0"))
        goal = str(request.get("goal", ""))
        tool = str(request.get("tool", "mock.echo"))
        target_path = request.get("target_path")
        return {
            "run_id": run_id,
            "provider": self.name,
            "proposed_tool": tool,
            "proposed_args": {"goal": goal, "summary": f"mock-executed:{goal}"},
            "target_path": target_path,
            "output_hash": canonical_json_hash({"goal": goal, "tool": tool}),
        }

    def cancel(self, run_id: str) -> bool:
        self.cancelled_runs.add(run_id)
        return True

    def quota(self) -> dict[str, Any]:
        return {"remaining": "unlimited-local", "cost_usd": 0.0}


class UnauthorizedCloudProvider:
    """Simulated cloud provider used to verify default-deny PolicyGate enforcement."""

    name: str = "cloud-unapproved"
    is_cloud: bool = True

    def capabilities(self) -> dict[str, Any]:
        return {"provider": self.name, "is_cloud": True}

    def infer(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("UnauthorizedCloudProvider.infer must never be reached without PolicyGate approval.")

    def cancel(self, run_id: str) -> bool:
        return False

    def quota(self) -> dict[str, Any]:
        return {"remaining": "unknown"}
