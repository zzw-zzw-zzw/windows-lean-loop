from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from lean_loop.agent_protocol import AgentRequest
from lean_loop.api import ApiError, extract_file_content, extract_json_object
from lean_loop.config import ApiConfig
from lean_loop.process_control import (
    ProcessCancelled,
    ProcessControl,
    run_controlled_process,
)


SUBSCRIPTION_BACKENDS = {"codex-subscription", "claude-subscription"}
MAX_DIAGNOSTIC_CHARS = 65536
_SECRET_ENV_PARTS = (
    "API_KEY",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "REFRESH_TOKEN",
    "AUTHORIZATION",
    "COOKIE",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "SESSION_TOKEN",
)
_SENSITIVE_ENV_PREFIXES = ("LEAN_AGENT_", "OPENAI_", "ANTHROPIC_", "CLAUDE_CODE_")
_EXTERNAL_OVERRIDE_ENV_PARTS = ("BASE_URL", "GATEWAY", "API_HOST", "ENDPOINT")
_REDACTIONS = (
    re.compile(r"(?i)\bBearer\s+[^\s\"']+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r'(?i)(\"?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|'
        r'authorization|cookie)\"?\s*[:=]\s*)\"?[^\s,}\"]+'
    ),
)
_CLAUDE_MODEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class SubscriptionBackendError(ApiError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        raw_output: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.raw_output = _sanitize(raw_output)
        self.metadata = dict(metadata or {})


def _sanitize(value: str) -> str:
    clean = value
    for pattern in _REDACTIONS:
        clean = pattern.sub(
            lambda match: (
                match.group(1) + "<redacted>" if match.lastindex else "<redacted>"
            ),
            clean,
        )
    return clean[:MAX_DIAGNOSTIC_CHARS]


def _safe_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for name in list(environment):
        upper = name.upper()
        if any(part in upper for part in _SECRET_ENV_PARTS):
            environment.pop(name, None)
        elif any(part in upper for part in _EXTERNAL_OVERRIDE_ENV_PARTS):
            environment.pop(name, None)
        elif any(upper.startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES):
            environment.pop(name, None)
        elif "SESSION" in upper and upper not in {"SESSIONNAME"}:
            environment.pop(name, None)
        elif upper in {"PWD", "OLDPWD", "PYTHONPATH", "VIRTUAL_ENV"}:
            environment.pop(name, None)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["NO_COLOR"] = "1"
    return environment


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_snapshot(root: Path | None, target: Path | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    if target is not None and target.is_file():
        snapshot["target_sha256"] = _sha256(target)
    if root is not None and (root / ".git").exists():
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        snapshot["git_status"] = completed.stdout
        snapshot["git_status_returncode"] = completed.returncode
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        snapshot["git_head"] = head.stdout.strip()
        snapshot["git_head_returncode"] = head.returncode
    return snapshot


def _classify_failure(stderr: str, stdout: str) -> str:
    value = f"{stderr}\n{stdout}".lower()
    if any(token in value for token in ("rate limit", "usage limit", "quota", "capacity")):
        return "usage_limit"
    if any(
        token in value
        for token in (
            "model is not available",
            "model not available",
            "unknown model",
            "unsupported model",
        )
    ):
        return "model_unavailable"
    if any(
        token in value
        for token in (
            "subscription unavailable",
            "request not allowed",
            "subscription is not available",
            "http 403",
            "status code 403",
        )
    ):
        return "subscription_unavailable"
    if any(
        token in value
        for token in ("not logged in", "login required", "authentication", "unauthorized")
    ):
        return "not_authenticated"
    return "nonzero_exit"


def _prompt(request: AgentRequest) -> str:
    output_contract = (
        "Return exactly one JSON object and no surrounding prose."
        if request.output_type == "json"
        else "Return the complete Lean source text with no Markdown fence or surrounding prose."
    )
    return json.dumps(
        {
            "protocol": request.protocol,
            "protocol_version": request.protocol_version,
            "role": request.role,
            "phase": request.phase,
            "output_type": request.output_type,
            "system_prompt": request.system_prompt,
            "user_prompt": request.user_prompt,
            "context": request.context,
            "output_contract": output_contract,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class _SubscriptionBackend:
    backend_id = ""
    executable = ""

    def __init__(
        self,
        *,
        executable: str | None = None,
        command_prefix: Sequence[str] | None = None,
        protected_root: Path | None = None,
        protected_target: Path | None = None,
        process_control: ProcessControl | None = None,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        selected = executable or self.executable
        self.command_prefix = tuple(command_prefix or (selected,))
        self.protected_root = protected_root.resolve() if protected_root else None
        self.protected_target = protected_target.resolve() if protected_target else None
        self.process_control = process_control
        self.base_environment = _safe_environment(base_environment)
        self.last_metadata: dict[str, Any] = {"backend_id": self.backend_id}
        self._ready: dict[tuple[str, str], dict[str, Any]] = {}

    def _command(self, *arguments: str) -> list[str]:
        return [*self.command_prefix, *arguments]

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        input_text: str | None = None,
        kind: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return run_controlled_process(
                self._command(*arguments),
                cwd=cwd,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
                kind=kind,
                control=self.process_control,
                env=_safe_environment(self.base_environment),
            )
        except FileNotFoundError as exc:
            raise SubscriptionBackendError(
                "cli_missing",
                f"{self.backend_id} CLI executable was not found",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SubscriptionBackendError(
                "timeout",
                f"{self.backend_id} timed out after {timeout_seconds} seconds",
                raw_output=f"{exc.stdout or ''}\n{exc.stderr or ''}",
                metadata=self.last_metadata,
            ) from exc

    def _probe(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        completed = self._run(
            arguments,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            kind=f"{self.backend_id}-probe",
        )
        if completed.returncode != 0:
            kind = _classify_failure(completed.stderr, completed.stdout)
            raise SubscriptionBackendError(
                kind,
                f"{self.backend_id} readiness probe failed",
                raw_output=f"{completed.stdout}\n{completed.stderr}",
                metadata=self.last_metadata,
            )
        return completed

    def _inspect(self, model: str, reasoning_effort: str, cwd: Path) -> dict[str, Any]:
        raise NotImplementedError

    def _arguments(
        self,
        *,
        model: str,
        reasoning_effort: str,
        cwd: Path,
        mcp_config: Path,
    ) -> list[str]:
        raise NotImplementedError

    def _parse(
        self,
        stdout: str,
        *,
        requested_model: str,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    def inspect(self, *, model: str, reasoning_effort: str) -> dict[str, Any]:
        if not model:
            raise SubscriptionBackendError(
                "model_identity_required",
                f"{self.backend_id} requires an explicit model",
            )
        if not reasoning_effort:
            raise SubscriptionBackendError(
                "unsupported_reasoning",
                f"{self.backend_id} requires an explicit reasoning effort",
            )
        key = (model, reasoning_effort)
        if key in self._ready:
            return dict(self._ready[key])
        with tempfile.TemporaryDirectory(prefix="windows-lean-loop-agent-probe-") as raw:
            report = self._inspect(model, reasoning_effort, Path(raw))
        self._ready[key] = dict(report)
        return report

    def invoke(
        self,
        request: AgentRequest,
        config: ApiConfig,
        temp_dir: Path,
    ) -> dict[str, Any] | str:
        del temp_dir
        reasoning = str(config.reasoning_effort or "")
        self.last_metadata = {
            "backend_id": self.backend_id,
            "requested_model": config.model,
            "requested_reasoning_effort": reasoning,
        }
        report = self.inspect(model=config.model, reasoning_effort=reasoning)
        metadata = {
            "backend_id": self.backend_id,
            "cli_version": report["cli_version"],
            "authentication_type": report["authentication_type"],
            "requested_model": config.model,
            "requested_reasoning_effort": reasoning,
            "effective_reasoning_effort": reasoning,
        }
        self.last_metadata = dict(metadata)
        before_project = _project_snapshot(self.protected_root, self.protected_target)
        with tempfile.TemporaryDirectory(prefix="windows-lean-loop-agent-") as raw:
            cwd = Path(raw).resolve()
            canary = cwd / "canary.txt"
            canary.touch()
            canary_sha = _sha256(canary)
            mcp_config = cwd / "empty-mcp.json"
            mcp_config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            expected_entries = sorted(path.name for path in cwd.iterdir())

            def side_effect_free() -> bool:
                return (
                    before_project
                    == _project_snapshot(self.protected_root, self.protected_target)
                    and canary.is_file()
                    and _sha256(canary) == canary_sha
                    and sorted(path.name for path in cwd.iterdir()) == expected_entries
                )

            arguments = self._arguments(
                model=config.model,
                reasoning_effort=reasoning,
                cwd=cwd,
                mcp_config=mcp_config,
            )
            try:
                completed = self._run(
                    arguments,
                    cwd=cwd,
                    timeout_seconds=config.timeout_seconds,
                    input_text=_prompt(request),
                    kind=self.backend_id,
                )
            except ProcessCancelled:
                terminal = "cancelled" if side_effect_free() else "side_effect_detected"
                self.last_metadata = {**metadata, "terminal_state": terminal}
                if terminal == "side_effect_detected":
                    raise SubscriptionBackendError(
                        terminal,
                        f"{self.backend_id} modified protected state while cancelling",
                        metadata=self.last_metadata,
                    )
                raise
            except SubscriptionBackendError:
                if not side_effect_free():
                    self.last_metadata = {
                        **metadata,
                        "terminal_state": "side_effect_detected",
                    }
                    raise SubscriptionBackendError(
                        "side_effect_detected",
                        f"{self.backend_id} modified protected state while failing",
                        metadata=self.last_metadata,
                    )
                raise
            if (
                len(completed.stdout) > MAX_DIAGNOSTIC_CHARS
                or len(completed.stderr) > MAX_DIAGNOSTIC_CHARS
            ):
                terminal = (
                    "output_too_large" if side_effect_free() else "side_effect_detected"
                )
                self.last_metadata = {
                    **metadata,
                    "terminal_state": terminal,
                    "exit_code": completed.returncode,
                }
                raise SubscriptionBackendError(
                    terminal,
                    f"{self.backend_id} exceeded the bounded output limit",
                    raw_output=f"{completed.stdout}\n{completed.stderr}",
                    metadata=self.last_metadata,
                )
            if completed.returncode != 0:
                kind = _classify_failure(completed.stderr, completed.stdout)
                self.last_metadata = {
                    **metadata,
                    "terminal_state": kind,
                    "exit_code": completed.returncode,
                }
                if not side_effect_free():
                    kind = "side_effect_detected"
                    self.last_metadata["terminal_state"] = kind
                raise SubscriptionBackendError(
                    kind,
                    f"{self.backend_id} exited with code {completed.returncode}",
                    raw_output=f"{completed.stdout}\n{completed.stderr}",
                    metadata=self.last_metadata,
                )
            try:
                final_text, parsed_metadata = self._parse(
                    completed.stdout,
                    requested_model=config.model,
                )
            except SubscriptionBackendError as exc:
                exc.metadata.update(metadata)
                exc.metadata["exit_code"] = completed.returncode
                self.last_metadata = dict(exc.metadata)
                if not side_effect_free():
                    self.last_metadata["terminal_state"] = "side_effect_detected"
                    raise SubscriptionBackendError(
                        "side_effect_detected",
                        f"{self.backend_id} modified protected state while parsing output",
                        metadata=self.last_metadata,
                    ) from exc
                raise
            if not side_effect_free():
                self.last_metadata = {
                    **metadata,
                    "exit_code": completed.returncode,
                    "terminal_state": "side_effect_detected",
                }
                raise SubscriptionBackendError(
                    "side_effect_detected",
                    f"{self.backend_id} modified protected state",
                    raw_output=f"{completed.stdout}\n{completed.stderr}",
                    metadata=self.last_metadata,
                )
            self.last_metadata = {
                **metadata,
                **parsed_metadata,
                "exit_code": completed.returncode,
                "output_type": request.output_type,
                "error_classification": None,
                "side_effect_free": True,
                "process_tree_cleaned": True,
                "terminal_state": "completed",
            }
        if request.output_type == "json":
            return extract_json_object(final_text)
        return extract_file_content(final_text)


class CodexSubscriptionBackend(_SubscriptionBackend):
    backend_id = "codex-subscription"
    executable = "codex"

    def _inspect(self, model: str, reasoning_effort: str, cwd: Path) -> dict[str, Any]:
        version = self._probe(("--version",), cwd=cwd).stdout.strip()
        root_help = self._probe(("--help",), cwd=cwd).stdout
        help_text = self._probe(("exec", "--help"), cwd=cwd).stdout
        required_flags = (
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "--model",
            "-C",
        )
        if "--ask-for-approval" not in root_help or not all(
            flag in help_text for flag in required_flags
        ):
            raise SubscriptionBackendError(
                "cli_version_unsupported",
                "Codex CLI does not expose the required non-interactive safety flags",
                raw_output=f"{root_help}\n{help_text}",
            )
        auth = self._probe(("login", "status"), cwd=cwd)
        auth_text = f"{auth.stdout}\n{auth.stderr}".lower()
        if "chatgpt" not in auth_text:
            raise SubscriptionBackendError(
                "not_authenticated",
                "Codex is not logged in with ChatGPT",
                raw_output=f"{auth.stdout}\n{auth.stderr}",
            )
        catalog = self._probe(("debug", "models"), cwd=cwd)
        try:
            models = json.loads(catalog.stdout).get("models", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise SubscriptionBackendError(
                "output_protocol_incompatible",
                "Codex model catalog was not valid JSON",
                raw_output=catalog.stdout,
            ) from exc
        row = next(
            (item for item in models if isinstance(item, dict) and item.get("slug") == model),
            None,
        )
        if row is None:
            raise SubscriptionBackendError(
                "model_unavailable",
                f"Codex model is not in the official catalog: {model}",
            )
        efforts = {
            str(item.get("effort"))
            for item in row.get("supported_reasoning_levels", [])
            if isinstance(item, dict)
        }
        if reasoning_effort not in efforts:
            raise SubscriptionBackendError(
                "unsupported_reasoning",
                f"Codex model {model} does not support reasoning effort {reasoning_effort}",
            )
        return {
            "status": "ready",
            "backend_id": self.backend_id,
            "cli_version": version,
            "authentication_type": "chatgpt",
            "model": model,
            "supported_reasoning_efforts": sorted(efforts),
        }

    def _arguments(
        self,
        *,
        model: str,
        reasoning_effort: str,
        cwd: Path,
        mcp_config: Path,
    ) -> list[str]:
        del mcp_config
        return [
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "-C",
            str(cwd),
            "-",
        ]

    def _parse(
        self,
        stdout: str,
        *,
        requested_model: str,
    ) -> tuple[str, dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SubscriptionBackendError(
                    "malformed_output",
                    "Codex returned malformed JSONL",
                    raw_output=stdout,
                ) from exc
            if not isinstance(value, dict):
                raise SubscriptionBackendError(
                    "malformed_output",
                    "Codex JSONL event must be an object",
                    raw_output=stdout,
                )
            events.append(value)
        final_messages = [
            str(event["item"].get("text") or "")
            for event in events
            if event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "agent_message"
        ]
        completed = sum(event.get("type") == "turn.completed" for event in events)
        failed = any(event.get("type") in {"turn.failed", "thread.failed"} for event in events)
        if failed or completed != 1 or len(final_messages) != 1 or not final_messages[0].strip():
            raise SubscriptionBackendError(
                (
                    "empty_output"
                    if not final_messages or not any(message.strip() for message in final_messages)
                    else "output_protocol_incompatible"
                ),
                "Codex did not return one completed final result",
                raw_output=stdout,
            )
        nonfatal = sum(
            event.get("type") == "error"
            or (
                event.get("type") == "item.completed"
                and isinstance(event.get("item"), dict)
                and event["item"].get("type") == "error"
            )
            for event in events
        )
        return final_messages[0], {
            "actual_model": requested_model,
            "model_identity_source": "explicit_official_catalog_no_fallback",
            "final_result_event": "turn.completed",
            "nonfatal_event_count": nonfatal,
        }


class ClaudeSubscriptionBackend(_SubscriptionBackend):
    backend_id = "claude-subscription"
    executable = "claude"

    def _inspect(self, model: str, reasoning_effort: str, cwd: Path) -> dict[str, Any]:
        version = self._probe(("--version",), cwd=cwd).stdout.strip()
        help_text = self._probe(("--help",), cwd=cwd).stdout
        required_flags = (
            "--output-format",
            "--model",
            "--effort",
            "--permission-mode",
            "--tools",
            "--no-session-persistence",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-chrome",
            "--strict-mcp-config",
            "--setting-sources",
            "--prompt-suggestions",
        )
        if not all(flag in help_text for flag in required_flags):
            raise SubscriptionBackendError(
                "cli_version_unsupported",
                "Claude CLI does not expose the required non-interactive safety flags",
                raw_output=help_text,
            )
        auth = self._probe(("auth", "status"), cwd=cwd)
        try:
            auth_value = json.loads(auth.stdout)
        except json.JSONDecodeError as exc:
            raise SubscriptionBackendError(
                "output_protocol_incompatible",
                "Claude auth status was not valid JSON",
                raw_output=auth.stdout,
            ) from exc
        authentication_type = str(auth_value.get("authMethod") or "")
        if (
            not auth_value.get("loggedIn")
            or authentication_type not in {"oauth_token", "claude.ai"}
            or auth_value.get("apiProvider") != "firstParty"
        ):
            raise SubscriptionBackendError(
                "not_authenticated",
                "Claude Code is not using first-party OAuth subscription login",
                raw_output=auth.stdout,
            )
        if not _CLAUDE_MODEL.fullmatch(model):
            raise SubscriptionBackendError(
                "model_identity_required",
                "Claude requires an explicit safe model identifier",
            )
        if reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise SubscriptionBackendError(
                "unsupported_reasoning",
                f"Claude does not support reasoning effort {reasoning_effort}",
            )
        return {
            "status": "ready",
            "backend_id": self.backend_id,
            "cli_version": version,
            "authentication_type": authentication_type,
            "model": model,
            "supported_reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
        }

    def _arguments(
        self,
        *,
        model: str,
        reasoning_effort: str,
        cwd: Path,
        mcp_config: Path,
    ) -> list[str]:
        del cwd
        return [
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--effort",
            reasoning_effort,
            "--permission-mode",
            "dontAsk",
            "--tools=",
            "--no-session-persistence",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_config),
            "--setting-sources",
            "local",
            "--prompt-suggestions",
            "false",
        ]

    def _parse(
        self,
        stdout: str,
        *,
        requested_model: str,
    ) -> tuple[str, dict[str, Any]]:
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SubscriptionBackendError(
                "malformed_output",
                "Claude returned malformed JSON",
                raw_output=stdout,
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("type") != "result"
            or value.get("subtype") != "success"
            or value.get("is_error") is not False
            or value.get("terminal_reason") != "completed"
        ):
            raise SubscriptionBackendError(
                "output_protocol_incompatible",
                "Claude result did not report successful completion",
                raw_output=stdout,
            )
        result = value.get("result")
        if not isinstance(result, str) or not result.strip():
            raise SubscriptionBackendError(
                "empty_output",
                "Claude returned an empty final result",
                raw_output=stdout,
            )
        usage = value.get("modelUsage")
        if not isinstance(usage, dict) or set(usage) != {requested_model}:
            raise SubscriptionBackendError(
                "model_identity_required",
                "Claude did not verify the requested model in modelUsage",
                raw_output=stdout,
            )
        return result, {
            "actual_model": requested_model,
            "model_identity_source": "result.modelUsage",
            "final_result_event": "result:success:completed",
            "nonfatal_event_count": 0,
        }


def inspect_subscription_backend(
    backend: _SubscriptionBackend,
    *,
    model: str,
    reasoning_effort: str,
) -> dict[str, Any]:
    return backend.inspect(model=model, reasoning_effort=reasoning_effort)


def create_subscription_backend(
    backend_id: str,
    *,
    protected_root: Path,
    protected_target: Path,
    process_control: ProcessControl | None = None,
) -> _SubscriptionBackend:
    if backend_id == "codex-subscription":
        return CodexSubscriptionBackend(
            protected_root=protected_root,
            protected_target=protected_target,
            process_control=process_control,
        )
    if backend_id == "claude-subscription":
        return ClaudeSubscriptionBackend(
            protected_root=protected_root,
            protected_target=protected_target,
            process_control=process_control,
        )
    raise ValueError(f"Unsupported subscription backend: {backend_id}")
