from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, runtime_checkable

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
DIAGNOSTIC_PREVIEW_BYTES = 65536
_REDACTED = "<redacted>"
_AMBIGUOUS_SENSITIVE_FIELD_NAMES = {"token", "secret"}
_SENSITIVE_FIELD_PARTS = (
    ("api", "key"),
    ("auth", "token"),
    ("session", "token"),
    ("access", "token"),
    ("refresh", "token"),
    ("client", "secret"),
    ("encrypted", "content"),
)
_SENSITIVE_FIELD_NAMES = {
    "authorization",
    "cookie",
    "credential",
    "password",
}
_SENSITIVE_FIELD_SEGMENTS = {
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
}
_BEARER_CREDENTIAL = re.compile(
    r"""(?ix)
    \bBearer[ \t]+
    (?:
        \\"(?P<escaped_double>(?:\\.|[^"\\])*)\\"
      | \\'(?P<escaped_single>(?:\\.|[^'\\])*)\\'
      | "(?P<double>(?:\\.|[^"\\])*)"
      | '(?P<single>(?:\\.|[^'\\])*)'
      | (?P<bare>[A-Za-z0-9._~+/=<>\-]{1,})
    )
    """
)
_CREDENTIAL_PATTERNS = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{16,}\b")),
    (
        "github_fine_grained_token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    ),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"
            r"\.[A-Za-z0-9_-]{5,}\b"
        ),
    ),
)
AgentRole = Literal[
    "formalizer", "planner", "prover", "reviewer", "auditor", "retriever", "explainer"
]
OutputType = Literal["json", "lean_file"]

JsonModelCall = Callable[[ApiConfig, str, str, Path], dict[str, Any]]
FileModelCall = Callable[[ApiConfig, str, Path], str]


class AgentProtocolError(ValueError):
    pass


class CredentialExposureError(AgentProtocolError):
    kind = "credential_exposure_detected"


def _normalize_field_name(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_sensitive_field_name(value: Any, *, allow_ambiguous: bool = True) -> bool:
    normalized = _normalize_field_name(value)
    if not normalized:
        return False
    if normalized in _SENSITIVE_FIELD_NAMES:
        return True
    if normalized in _AMBIGUOUS_SENSITIVE_FIELD_NAMES:
        return allow_ambiguous
    parts = tuple(part for part in normalized.split("_") if part)
    if any(part in _SENSITIVE_FIELD_SEGMENTS for part in parts):
        return True
    return any(
        parts[index : index + len(marker)] == marker
        for marker in _SENSITIVE_FIELD_PARTS
        for index in range(len(parts) - len(marker) + 1)
    )


def _is_exact_redacted_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1]:
        if normalized[0] in {'"', "'"}:
            normalized = normalized[1:-1].strip()
    return normalized == _REDACTED


def _bearer_value(match: re.Match[str]) -> str:
    return next(
        (
            value
            for name in (
                "escaped_double",
                "escaped_single",
                "double",
                "single",
                "bare",
            )
            if (value := match.groupdict().get(name)) is not None
        ),
        "",
    )


def _text_credential_category(
    value: str, *, text_context: str = "generic"
) -> str | None:
    del text_context
    for match in _BEARER_CREDENTIAL.finditer(value):
        if not _is_exact_redacted_value(_bearer_value(match)):
            return "bearer_token"
    for category, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(value):
            return category
    return None


def find_high_confidence_credential(
    value: Any,
    *,
    text_context: str = "generic",
    inspect_sensitive_fields: bool = False,
) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if inspect_sensitive_fields and is_sensitive_field_name(key):
                if item not in (None, "") and not _is_exact_redacted_value(item):
                    return "sensitive_field"
                continue
            finding = find_high_confidence_credential(
                item,
                text_context=text_context,
                inspect_sensitive_fields=inspect_sensitive_fields,
            )
            if finding is not None:
                return finding
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            finding = find_high_confidence_credential(
                item,
                text_context=text_context,
                inspect_sensitive_fields=inspect_sensitive_fields,
            )
            if finding is not None:
                return finding
        return None
    if isinstance(value, str):
        return _text_credential_category(value, text_context=text_context)
    return None


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
            finding = find_high_confidence_credential(
                value, inspect_sensitive_fields=True
            )
            if finding is None:
                return dict(value)
            return {
                "backend_id": str(getattr(self.backend, "backend_id", "unknown")),
                "metadata_redaction_applied": True,
                "metadata_exposure_category": finding,
            }
        return {"backend_id": str(getattr(self.backend, "backend_id", "unknown"))}

    @staticmethod
    def _response_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        compact = dict(metadata)
        if isinstance(compact.pop("tool_events", None), list):
            compact["tool_events_saved_separately"] = True
        return compact

    def _backend_streams_preprocessed(self) -> bool:
        metadata = getattr(self.backend, "last_metadata", None)
        if not isinstance(metadata, dict):
            return False
        streams = metadata.get("stream_evidence")
        if not isinstance(streams, dict):
            return False
        raw_output = streams.get("raw_output")
        return (
            isinstance(raw_output, dict)
            and raw_output.get("contains_unredacted_process_stream") is False
        )

    @staticmethod
    def _stream_preview_truncated(metadata: dict[str, Any]) -> bool:
        streams = metadata.get("stream_evidence")
        if not isinstance(streams, dict):
            return False
        preview = streams.get("diagnostic_preview")
        return (
            bool(preview.get("preview_truncated"))
            if isinstance(preview, dict)
            else False
        )

    def _persist_backend_streams(self, call_dir: Path) -> bool:
        stdout = getattr(self.backend, "last_stdout", None)
        stderr = getattr(self.backend, "last_stderr", None)
        diagnostic_preview = getattr(self.backend, "last_diagnostic_preview", None)
        if not isinstance(stdout, str) and not isinstance(stderr, str):
            return False
        stdout_text = stdout if isinstance(stdout, str) else ""
        stderr_text = stderr if isinstance(stderr, str) else ""
        if (
            not self._backend_streams_preprocessed()
            and find_high_confidence_credential((stdout_text, stderr_text)) is not None
        ):
            return False
        atomic_write_text(call_dir / "stdout.txt", stdout_text)
        atomic_write_text(call_dir / "stderr.txt", stderr_text)
        if isinstance(diagnostic_preview, str):
            atomic_write_text(
                call_dir / "diagnostic-preview.txt", diagnostic_preview
            )
        return True

    @staticmethod
    def _bounded_diagnostic_preview(value: str) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= DIAGNOSTIC_PREVIEW_BYTES:
            return value, False
        preview = encoded[:DIAGNOSTIC_PREVIEW_BYTES].decode(
            "utf-8", errors="ignore"
        )
        return preview, True

    @staticmethod
    def _persist_backend_evidence(call_dir: Path, metadata: dict[str, Any]) -> None:
        if find_high_confidence_credential(
            metadata, inspect_sensitive_fields=True
        ) is not None:
            return
        tool_events = metadata.get("tool_events")
        if isinstance(tool_events, list):
            atomic_write_json(call_dir / "tool-events.json", {"events": tool_events})
        sandbox_manifest = metadata.get("sandbox_manifest")
        if isinstance(sandbox_manifest, dict):
            atomic_write_json(call_dir / "sandbox-manifest.json", sandbox_manifest)

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
            credential_category = find_high_confidence_credential(
                output,
                text_context=(
                    "lean_source" if output_type == "lean_file" else "generic"
                ),
            )
            if credential_category is not None:
                raise CredentialExposureError(
                    "Agent output contained high-confidence credential material"
                )
        except Exception as exc:
            raised_exception: Exception = exc
            raw_output = getattr(exc, "raw_output", None)
            error_kind = getattr(exc, "kind", None)
            exception_finding = find_high_confidence_credential(
                (str(exc), raw_output if isinstance(raw_output, str) else "")
            )
            if exception_finding is not None:
                error_kind = CredentialExposureError.kind
                raw_output = None
                error_message = (
                    "Agent call contained high-confidence credential material"
                )
                if not isinstance(exc, CredentialExposureError):
                    raised_exception = CredentialExposureError(error_message)
            else:
                error_message = str(exc)
            backend_metadata = self._backend_metadata()
            self._persist_backend_evidence(call_dir, backend_metadata)
            response_metadata = self._response_metadata(backend_metadata)
            raw_streams_saved = self._persist_backend_streams(call_dir)
            raw_output_saved = raw_streams_saved
            diagnostic_preview_saved = raw_streams_saved and (
                call_dir / "diagnostic-preview.txt"
            ).is_file()
            diagnostic_preview_truncated = self._stream_preview_truncated(
                backend_metadata
            )
            if not raw_streams_saved and isinstance(raw_output, str) and raw_output:
                diagnostic_preview, diagnostic_preview_truncated = (
                    self._bounded_diagnostic_preview(raw_output)
                )
                atomic_write_text(
                    call_dir / "diagnostic-preview.txt", diagnostic_preview
                )
                diagnostic_preview_saved = True
                if not diagnostic_preview_truncated:
                    atomic_write_text(
                        call_dir / "raw-output.txt", diagnostic_preview
                    )
                    raw_output_saved = True
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
                    "type": type(raised_exception).__name__,
                    "message": error_message,
                    **({"kind": error_kind} if isinstance(error_kind, str) else {}),
                },
                metadata={
                    "model": config.model,
                    "raw_output_saved": raw_output_saved,
                    "diagnostic_preview_saved": diagnostic_preview_saved,
                    "diagnostic_preview_truncated": (
                        diagnostic_preview_truncated
                    ),
                    **response_metadata,
                    **(
                        {"error_classification": error_kind}
                        if isinstance(error_kind, str)
                        else {}
                    ),
                },
            )
            atomic_write_json(call_dir / "response.json", response.to_dict())
            if raised_exception is not exc:
                raise raised_exception from exc
            raise
        backend_metadata = self._backend_metadata()
        self._persist_backend_evidence(call_dir, backend_metadata)
        response_metadata = self._response_metadata(backend_metadata)
        raw_streams_saved = self._persist_backend_streams(call_dir)
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
                "raw_output_saved": raw_streams_saved,
                "diagnostic_preview_saved": (
                    call_dir / "diagnostic-preview.txt"
                ).is_file(),
                "diagnostic_preview_truncated": (
                    self._stream_preview_truncated(backend_metadata)
                ),
                **response_metadata,
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
