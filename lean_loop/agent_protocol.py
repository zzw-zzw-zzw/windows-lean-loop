from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from lean_loop.config import ApiConfig
from lean_loop.jsonutil import atomic_write_json, atomic_write_text, utc_now


PROTOCOL_NAME = "lean-agent"
PROTOCOL_VERSION = 1
AGENT_ROLES = {
    "formalizer",
    "planner",
    "prover",
    "reviewer",
    "auditor",
    "retriever",
    "explainer",
}
OUTPUT_TYPES = {"json", "lean_file"}

AgentRole = Literal[
    "formalizer", "planner", "prover", "reviewer", "auditor", "retriever", "explainer"
]
OutputType = Literal["json", "lean_file"]

JsonModelCall = Callable[[ApiConfig, str, str, Path], dict[str, Any]]
FileModelCall = Callable[[ApiConfig, str, Path], str]


class AgentProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class AgentRequest:
    request_id: str
    sequence: int
    role: AgentRole
    run_id: str
    phase: str
    output_type: OutputType
    model: str
    reasoning_effort: str | None
    system_prompt: str
    user_prompt: str
    attempt: int | None = None
    step_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    protocol: str = PROTOCOL_NAME
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol != PROTOCOL_NAME or self.protocol_version != PROTOCOL_VERSION:
            raise AgentProtocolError("Unsupported Agent protocol version")
        if self.role not in AGENT_ROLES:
            raise AgentProtocolError(f"Unsupported Agent role: {self.role}")
        if self.output_type not in OUTPUT_TYPES:
            raise AgentProtocolError(f"Unsupported Agent output type: {self.output_type}")
        if not self.request_id or self.sequence < 1 or not self.run_id or not self.phase:
            raise AgentProtocolError("Agent request identity fields are required")
        if not self.system_prompt or not self.user_prompt:
            raise AgentProtocolError("Agent prompts must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentResponse:
    request_id: str
    role: AgentRole
    status: Literal["ok", "error"]
    output_type: OutputType
    output: dict[str, Any] | str | None
    started_at: str
    completed_at: str
    duration_seconds: float
    error: dict[str, str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    protocol: str = PROTOCOL_NAME
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.status == "ok" and self.output is None:
            raise AgentProtocolError("Successful Agent response requires output")
        if self.status == "error" and not self.error:
            raise AgentProtocolError("Failed Agent response requires an error")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class AgentBackend(Protocol):
    def invoke(
        self,
        request: AgentRequest,
        config: ApiConfig,
        temp_dir: Path,
    ) -> dict[str, Any] | str:
        """Execute one versioned Agent request without mutating workflow state."""


class DirectModelBackend:
    backend_id = "direct"

    def __init__(
        self,
        *,
        json_model_call: JsonModelCall | None,
        file_model_call: FileModelCall | None,
    ) -> None:
        self.json_model_call = json_model_call
        self.file_model_call = file_model_call
        self.last_metadata: dict[str, Any] = {"backend_id": self.backend_id}

    def invoke(
        self,
        request: AgentRequest,
        config: ApiConfig,
        temp_dir: Path,
    ) -> dict[str, Any] | str:
        self.last_metadata = {
            "backend_id": self.backend_id,
            "requested_model": config.model,
            "actual_model": config.model,
            "requested_reasoning_effort": config.reasoning_effort,
            "effective_reasoning_effort": config.reasoning_effort,
        }
        if request.output_type == "json":
            if self.json_model_call is None:
                raise AgentProtocolError("Backend does not support JSON Agent output")
            return self.json_model_call(
                config,
                request.system_prompt,
                request.user_prompt,
                temp_dir,
            )
        if self.file_model_call is None:
            raise AgentProtocolError("Backend does not support Lean-file Agent output")
        return self.file_model_call(config, request.user_prompt, temp_dir)


class AgentRuntime:
    def __init__(
        self,
        *,
        workflow_root: Path,
        run_id: str,
        backend: AgentBackend,
    ) -> None:
        self.root = workflow_root / "agent-calls"
        self.run_id = run_id
        self.backend = backend
        self.root.mkdir(parents=True, exist_ok=True)
        self._sequence = max(
            (
                int(path.name.split("-", 1)[0])
                for path in self.root.iterdir()
                if path.is_dir() and path.name.split("-", 1)[0].isdigit()
            ),
            default=0,
        )

    def _backend_metadata(self) -> dict[str, Any]:
        value = getattr(self.backend, "last_metadata", None)
        if isinstance(value, dict):
            return dict(value)
        return {"backend_id": str(getattr(self.backend, "backend_id", "unknown"))}

    def invoke(
        self,
        *,
        role: AgentRole,
        phase: str,
        output_type: OutputType,
        config: ApiConfig,
        system_prompt: str,
        user_prompt: str,
        temp_dir: Path,
        attempt: int | None = None,
        step_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | str:
        self._sequence += 1
        request_id = uuid.uuid4().hex
        request = AgentRequest(
            request_id=request_id,
            sequence=self._sequence,
            role=role,
            run_id=self.run_id,
            phase=phase,
            output_type=output_type,
            model=config.model,
            reasoning_effort=config.reasoning_effort,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            attempt=attempt,
            step_id=step_id,
            context=dict(context or {}),
        )
        call_dir = self.root / f"{self._sequence:04d}-{role}-{request_id[:8]}"
        call_dir.mkdir(parents=True, exist_ok=False)
        atomic_write_json(call_dir / "request.json", request.to_dict())
        started_at = utc_now()
        started = time.perf_counter()
        try:
            output = self.backend.invoke(request, config, temp_dir)
            if output_type == "json" and not isinstance(output, dict):
                raise AgentProtocolError("JSON Agent response must be an object")
            if output_type == "lean_file" and not isinstance(output, str):
                raise AgentProtocolError("Lean-file Agent response must be text")
        except Exception as exc:
            raw_output = getattr(exc, "raw_output", None)
            error_kind = getattr(exc, "kind", None)
            if isinstance(raw_output, str) and raw_output:
                atomic_write_text(call_dir / "raw-output.txt", raw_output[: 1024 * 1024])
            response = AgentResponse(
                request_id=request_id,
                role=role,
                status="error",
                output_type=output_type,
                output=None,
                started_at=started_at,
                completed_at=utc_now(),
                duration_seconds=round(time.perf_counter() - started, 6),
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    **({"kind": error_kind} if isinstance(error_kind, str) else {}),
                },
                metadata={
                    "model": config.model,
                    "raw_output_saved": bool(isinstance(raw_output, str) and raw_output),
                    **self._backend_metadata(),
                    **(
                        {"error_classification": error_kind}
                        if isinstance(error_kind, str)
                        else {}
                    ),
                },
            )
            atomic_write_json(call_dir / "response.json", response.to_dict())
            raise
        response = AgentResponse(
            request_id=request_id,
            role=role,
            status="ok",
            output_type=output_type,
            output=output,
            started_at=started_at,
            completed_at=utc_now(),
            duration_seconds=round(time.perf_counter() - started, 6),
            metadata={
                "model": config.model,
                "reasoning_effort": config.reasoning_effort,
                **self._backend_metadata(),
            },
        )
        atomic_write_json(call_dir / "response.json", response.to_dict())
        return output


def protocol_capabilities() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "roles": sorted(AGENT_ROLES),
        "output_types": sorted(OUTPUT_TYPES),
        "features": [
            "versioned_requests",
            "versioned_responses",
            "persistent_call_artifacts",
            "role_specific_models",
            "role_specific_reasoning_effort",
            "replaceable_backend",
            "resume_safe_sequence",
        ],
    }
