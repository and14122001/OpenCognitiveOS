"""Default-deny PolicyGate for providers, tools, and paths (M1.2 offline baseline).

M1 Limitations (explicitly deferred to M2):
- Path checks validate lexical safety and exact path-component boundaries against
  `TaskSpec.allowed_paths` (without `lstrip("./")`).
- Filesystem symlink resolution and OS-level Sandbox isolation are NOT implemented in M1
  and remain Pending for M2.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from opencognitiveos.contracts.models import Provider, TaskSpec
from opencognitiveos.errors import PolicyDeniedError

_PATH_ARG_KEYS = ("path", "target_path", "file_path", "dest", "src", "paths")


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


class PolicyGate:
    """Enforces authorization rules equally across Mock and real providers."""

    def check_provider(self, spec: TaskSpec, provider: Provider) -> PolicyDecision:
        # M1 hard rule: real cloud providers are unconditionally disabled (no runtime bypass).
        if provider.is_cloud:
            raise PolicyDeniedError(
                f"Cloud provider '{provider.name}' denied: real cloud providers are unconditionally disabled in M1."
            )

        if provider.name not in spec.allowed_providers:
            raise PolicyDeniedError(
                f"Provider '{provider.name}' is not in allowed_providers {spec.allowed_providers}."
            )
        return PolicyDecision(allowed=True, reason=f"Provider '{provider.name}' authorized.")

    @staticmethod
    def _normalize_relative_parts(raw_path: str) -> tuple[str, ...]:
        if not raw_path or "\x00" in raw_path:
            raise PolicyDeniedError("Path denied: empty or contains null byte.")
        if "\\" in raw_path:
            raise PolicyDeniedError(f"Path '{raw_path}' denied: backslashes are not permitted.")
        if raw_path.startswith("/") or (len(raw_path) >= 2 and raw_path[1] == ":"):
            raise PolicyDeniedError(f"Path '{raw_path}' denied: absolute paths are forbidden.")

        # Strip ONLY an exact leading "./" prefix if present; never use lstrip("./")!
        candidate = raw_path
        while candidate.startswith("./"):
            candidate = candidate[2:]
        if not candidate or candidate == ".":
            raise PolicyDeniedError(f"Path '{raw_path}' denied: empty or root '.' path.")

        parts = PurePosixPath(candidate).parts
        if not parts:
            raise PolicyDeniedError(f"Path '{raw_path}' denied: no path components.")
        for part in parts:
            if part in ("", ".", "..", "/"):
                raise PolicyDeniedError(
                    f"Path '{raw_path}' denied: invalid or traversal component '{part}'."
                )
        return parts

    @classmethod
    def _validate_relative_path(cls, raw_path: str, allowed_paths: tuple[str, ...]) -> tuple[str, ...]:
        parts = cls._normalize_relative_parts(raw_path)
        if not allowed_paths:
            raise PolicyDeniedError(f"Path '{raw_path}' denied: TaskSpec.allowed_paths is empty.")

        for prefix in allowed_paths:
            prefix_parts = cls._normalize_relative_parts(prefix)
            if len(parts) >= len(prefix_parts) and parts[: len(prefix_parts)] == prefix_parts:
                return parts

        raise PolicyDeniedError(f"Path '{raw_path}' is outside allowed_paths {allowed_paths}.")

    @classmethod
    def _extract_paths_from_args(cls, args: dict[str, Any] | None) -> list[str]:
        if not args:
            return []
        found: list[str] = []
        for key in _PATH_ARG_KEYS:
            val = args.get(key)
            if isinstance(val, str):
                found.append(val)
            elif isinstance(val, (list, tuple)):
                for item in val:
                    if isinstance(item, str):
                        found.append(item)
        return found

    def check_tool(
        self,
        spec: TaskSpec,
        tool_name: str,
        *,
        args: dict[str, Any] | None = None,
        target_path: str | None = None,
    ) -> PolicyDecision:
        if tool_name not in spec.allowed_tools:
            raise PolicyDeniedError(
                f"Tool '{tool_name}' is not in allowed_tools {spec.allowed_tools}."
            )

        arg_paths = self._extract_paths_from_args(args)
        if target_path is not None and arg_paths and target_path not in arg_paths:
            raise PolicyDeniedError(
                f"Path mismatch: declared target_path '{target_path}' disagrees with tool args paths {arg_paths}."
            )

        paths_to_check = list(arg_paths)
        if target_path is not None and target_path not in paths_to_check:
            paths_to_check.append(target_path)

        for p in paths_to_check:
            self._validate_relative_path(p, spec.allowed_paths)

        return PolicyDecision(allowed=True, reason=f"Tool '{tool_name}' authorized.")
