from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from lean_loop.agent_protocol import (
    AgentRequest,
    find_high_confidence_credential,
    is_sensitive_field_name,
)
from lean_loop.api import ApiError, extract_file_content, extract_json_object
from lean_loop.config import ApiConfig
from lean_loop.process_control import (
    ProcessCancelled,
    ProcessControl,
    ProcessOutputLimitExceeded,
    run_controlled_process,
)


SUBSCRIPTION_BACKENDS = {"codex-subscription", "claude-subscription"}
MAX_DIAGNOSTIC_CHARS = 65536
MAX_DIAGNOSTIC_BYTES = 65536
CODEX_MAX_PROCESS_OUTPUT_BYTES = 2 * 1024 * 1024
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
_REDACTED = "<redacted>"
_BEARER_REDACTION = re.compile(
    r"""(?ix)
    (?P<prefix>\bBearer[ \t]+)
    (?:
        \\"(?P<escaped_double>(?:\\.|[^"\\])*)\\"
      | \\'(?P<escaped_single>(?:\\.|[^'\\])*)\\'
      | "(?P<double>(?:\\.|[^"\\])*)"
      | '(?P<single>(?:\\.|[^'\\])*)'
      | (?P<bare>[A-Za-z0-9._~+/=<>\-]{1,})
    )
    """
)
_SK_REDACTION = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_STANDALONE_TOKEN_REDACTIONS = (
    re.compile(r"\bghp_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"
        r"\.[A-Za-z0-9_-]{5,}\b"
    ),
)
_DOUBLE_QUOTED_FIELD_REDACTION = re.compile(
    r'(?P<prefix>(?:"(?P<quoted_key>[^"\\]{1,128})"|'
    r'(?P<bare_key>[A-Za-z_][A-Za-z0-9_.-]{0,127}))\s*[:=]\s*")'
    r'(?P<value>(?:\\.|[^"\\])*)(?P<suffix>")'
)
_SINGLE_QUOTED_FIELD_REDACTION = re.compile(
    r"(?P<prefix>(?:'(?P<quoted_key>[^'\\]{1,128})'|"
    r"(?P<bare_key>[A-Za-z_][A-Za-z0-9_.-]{0,127}))\s*[:=]\s*')"
    r"(?P<value>(?:\\.|[^'\\])*)(?P<suffix>')"
)
_UNQUOTED_FIELD_PREFIX = re.compile(
    r'(?:"(?P<double_quoted_key>[^"\\]{1,128})"|'
    r"'(?P<single_quoted_key>[^'\\]{1,128})'|"
    r"(?P<bare_key>\b[A-Za-z_][A-Za-z0-9_.-]{0,127}\b))"
    r"[^\S\r\n]*[:=]"
)
_CLAUDE_MODEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
CODEX_TOOL_EXECUTION_POLICY = "TOOL_ENABLED_AGENT_SANDBOX"
_SANDBOX_SCOPE_METADATA_FIELDS = (
    "filesystem_read_scope",
    "filesystem_write_scope",
    "read_isolation_status",
    "network_policy",
)
CODEX_SANDBOX_SCOPE = {
    "filesystem_read_scope": "WINDOWS_BROAD_READ",
    "filesystem_write_scope": "REPO_EXTERNAL_EPHEMERAL_WORKSPACE",
    "read_isolation_status": "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX",
    "network_policy": "DISABLED",
}
CODEX_SANDBOX_PROFILE = {
    "approval_policy": "never",
    "filesystem": "workspace-write",
    "isolation": "repo-external-ephemeral",
    "temp_environment_write_access": "disabled",
    "protected_state_policy": "snapshot-fail-closed",
    "session_policy": "ephemeral",
    **CODEX_SANDBOX_SCOPE,
}
CLAUDE_TOOL_EXECUTION_POLICY = "TOOLS_DISABLED_BY_CLIENT_FLAGS"
CLAUDE_SANDBOX_PROFILE = {
    "filesystem": "safe-mode",
    "isolation": "repo-external-ephemeral",
    "network_policy": "client-managed",
    "protected_state_policy": "snapshot-fail-closed",
    "session_policy": "no-session-persistence",
}
_CODEX_TOOL_EVENT_TYPES = {
    "apply_patch",
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
}


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
        self.raw_output = _diagnostic_preview(raw_output)
        self.metadata = dict(metadata or {})


def _redact_bearer(match: re.Match[str]) -> str:
    prefix = match.group("prefix")
    if match.group("escaped_double") is not None:
        return prefix + '\\"' + _REDACTED + '\\"'
    if match.group("escaped_single") is not None:
        return prefix + "\\'" + _REDACTED + "\\'"
    if match.group("double") is not None:
        return prefix + '"' + _REDACTED + '"'
    if match.group("single") is not None:
        return prefix + "'" + _REDACTED + "'"
    return prefix + _REDACTED


def _redact_standalone_credentials(value: str) -> str:
    clean = _BEARER_REDACTION.sub(_redact_bearer, value)
    clean = _SK_REDACTION.sub(_REDACTED, clean)
    for pattern in _STANDALONE_TOKEN_REDACTIONS:
        clean = pattern.sub(_REDACTED, clean)
    return clean


def _redact_assignments(value: str) -> str:
    clean = value
    for pattern in (
        _DOUBLE_QUOTED_FIELD_REDACTION,
        _SINGLE_QUOTED_FIELD_REDACTION,
    ):
        clean = pattern.sub(
            _redact_text_field,
            clean,
        )
    clean = _redact_unquoted_sensitive_fields(clean)
    return clean


def _redact_text(
    value: str, *, text_context: str = "generic"
) -> tuple[str, bool]:
    clean = (
        _redact_standalone_credentials(value)
        if text_context == "lean_source"
        else _redact_assignments(_redact_standalone_credentials(value))
    )
    return clean, clean != value


def _redact(value: str) -> str:
    return _redact_text(value)[0]


def _redact_text_field(match: re.Match[str]) -> str:
    key = match.groupdict().get("quoted_key") or match.groupdict().get("bare_key")
    if not is_sensitive_field_name(key):
        return match.group(0)
    if str(match.groupdict().get("value") or "").strip() == _REDACTED:
        return match.group(0)
    return (
        match.group("prefix")
        + _REDACTED
        + (match.groupdict().get("suffix") or "")
    )


def _closing_quote(value: str, start: int, quote: str) -> int | None:
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            return index
    return None


def _field_value_is_exact_redacted(
    value: str,
    *,
    value_start: int,
    active_quote: str | None,
) -> bool:
    if active_quote is not None:
        value_end = _closing_quote(value, value_start, active_quote)
        field_value = value[
            value_start : len(value) if value_end is None else value_end
        ]
        return field_value.strip() == _REDACTED
    remainder = value[value_start:]
    if remainder.startswith(('"', "'")):
        quote = remainder[0]
        value_end = _closing_quote(value, value_start + 1, quote)
        if value_end is None:
            return False
        return value[value_start + 1 : value_end].strip() == _REDACTED
    return remainder.strip() == _REDACTED


def _redact_unquoted_sensitive_fields(value: str) -> str:
    saved_lines: list[str] = []
    for chunk in value.splitlines(keepends=True):
        if chunk.endswith("\r\n"):
            line, ending = chunk[:-2], "\r\n"
        elif chunk.endswith(("\r", "\n")):
            line, ending = chunk[:-1], chunk[-1:]
        else:
            line, ending = chunk, ""
        saved_parts: list[str] = []
        cursor = 0
        quote_cursor = 0
        active_quote: str | None = None
        escaped = False
        for match in _UNQUOTED_FIELD_PREFIX.finditer(line):
            for char in line[quote_cursor : match.start()]:
                if escaped:
                    escaped = False
                    continue
                if char == "\\":
                    escaped = True
                    continue
                if active_quote is None:
                    if char in {'"', "'"}:
                        active_quote = char
                elif char == active_quote:
                    active_quote = None
            quote_cursor = match.start()
            if match.start() < cursor:
                continue
            key = (
                match.group("double_quoted_key")
                or match.group("single_quoted_key")
                or match.group("bare_key")
            )
            normalized_key = re.sub(
                r"[^a-z0-9]+",
                "_",
                re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower(),
            ).strip("_")
            if normalized_key != "secret" and not is_sensitive_field_name(
                key, allow_ambiguous=False
            ):
                continue
            remainder = line[match.end() :]
            leading_space = remainder[: len(remainder) - len(remainder.lstrip(" \t"))]
            value_start = match.end() + len(leading_space)
            if _field_value_is_exact_redacted(
                line,
                value_start=value_start,
                active_quote=active_quote,
            ):
                continue
            value_end = (
                _closing_quote(line, value_start, active_quote)
                if active_quote is not None
                else len(line)
            )
            if value_end is None:
                value_end = len(line)
            saved_parts.append(line[cursor:match.end()])
            saved_parts.append(leading_space)
            saved_parts.append(_REDACTED)
            cursor = value_end
            if active_quote is None:
                break
        saved_parts.append(line[cursor:])
        saved_lines.append("".join(saved_parts) + ending)
    return "".join(saved_lines)


def _redact_json_value(
    value: Any, *, text_context: str = "generic"
) -> tuple[Any, bool]:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            if is_sensitive_field_name(key):
                redacted[key] = _REDACTED
                changed = True
            else:
                redacted_item, item_changed = _redact_json_value(
                    item, text_context=text_context
                )
                redacted[key] = redacted_item
                changed = changed or item_changed
        return redacted, changed
    if isinstance(value, list):
        redacted_items: list[Any] = []
        changed = False
        for item in value:
            redacted_item, item_changed = _redact_json_value(
                item, text_context=text_context
            )
            redacted_items.append(redacted_item)
            changed = changed or item_changed
        return redacted_items, changed
    if isinstance(value, str):
        return _redact_text(value, text_context=text_context)
    return value, False


def _redact_codex_event(
    event: Mapping[str, Any], *, output_type: str | None
) -> tuple[dict[Any, Any], bool]:
    redacted: dict[Any, Any] = {}
    changed = False
    agent_item = event.get("item")
    for key, item in event.items():
        if is_sensitive_field_name(key):
            redacted[key] = _REDACTED
            changed = True
            continue
        if (
            key == "item"
            and isinstance(agent_item, Mapping)
            and agent_item.get("type") == "agent_message"
        ):
            saved_item: dict[Any, Any] = {}
            item_changed = False
            for item_key, item_value in agent_item.items():
                if is_sensitive_field_name(item_key):
                    saved_item[item_key] = _REDACTED
                    item_changed = True
                elif item_key == "text" and isinstance(item_value, str):
                    saved_value, value_changed = _redact_text(
                        item_value,
                        text_context=(
                            "lean_source"
                            if output_type == "lean_file"
                            else "generic"
                        ),
                    )
                    saved_item[item_key] = saved_value
                    item_changed = item_changed or value_changed
                else:
                    saved_value, value_changed = _redact_json_value(item_value)
                    saved_item[item_key] = saved_value
                    item_changed = item_changed or value_changed
            redacted[key] = saved_item
            changed = changed or item_changed
            continue
        saved_value, value_changed = _redact_json_value(item)
        redacted[key] = saved_value
        changed = changed or value_changed
    return redacted, changed


def _redact_codex_stdout(
    value: str, *, output_type: str | None = None
) -> tuple[str, bool]:
    if not value:
        return "", False
    has_terminal_newline = value.endswith("\n")
    lines = value.split("\n")
    if has_terminal_newline:
        lines.pop()
    saved_lines: list[str] = []
    redaction_applied = False
    for raw_line in lines:
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            saved_line, changed = _redact_text(line)
        else:
            if isinstance(event, Mapping):
                redacted_event, changed = _redact_codex_event(
                    event, output_type=output_type
                )
            else:
                redacted_event, changed = _redact_json_value(event)
            saved_line = json.dumps(
                redacted_event,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        saved_lines.append(saved_line)
        redaction_applied = redaction_applied or changed
    saved = "\n".join(saved_lines)
    if has_terminal_newline:
        saved += "\n"
    return saved, redaction_applied


def _codex_events_credential_category(
    events: Sequence[Mapping[str, Any]], *, output_type: str
) -> str | None:
    for event in events:
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            for key, value in event.items():
                if key == "item":
                    continue
                finding = find_high_confidence_credential({key: value})
                if finding is not None:
                    return finding
            for key, value in item.items():
                if key == "text" and isinstance(value, str):
                    finding = find_high_confidence_credential(
                        value,
                        text_context=(
                            "lean_source"
                            if output_type == "lean_file"
                            else "generic"
                        ),
                    )
                else:
                    finding = find_high_confidence_credential({key: value})
                if finding is not None:
                    return finding
            continue
        finding = find_high_confidence_credential(event)
        if finding is not None:
            return finding
    return None


def _diagnostic_preview(value: str) -> str:
    clean = _redact(value)
    return _bounded_redacted_preview(clean)


def _bounded_redacted_preview(value: str) -> str:
    clean = value[:MAX_DIAGNOSTIC_CHARS]
    encoded = clean.encode("utf-8")
    if len(encoded) <= MAX_DIAGNOSTIC_BYTES:
        return clean
    return encoded[:MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")


def _sanitize(value: str) -> str:
    """Return a bounded, redacted diagnostic string."""
    return _diagnostic_preview(value)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stream_record(
    process_stream: str,
    saved_stream: str,
    *,
    redaction_applied: bool,
    complete_process_stream_observed: bool,
    complete_captured_prefix_saved: bool,
) -> dict[str, Any]:
    return {
        "process_stream": {
            "scope": (
                "complete_process_stream"
                if complete_process_stream_observed
                else "captured_prefix_only"
            ),
            "char_count": len(process_stream),
            "byte_count": len(process_stream.encode("utf-8")),
            "sha256": _text_sha256(process_stream),
            "complete_process_stream_observed": complete_process_stream_observed,
        },
        "saved_redacted_evidence": {
            "char_count": len(saved_stream),
            "byte_count": len(saved_stream.encode("utf-8")),
            "sha256": _text_sha256(saved_stream),
            "complete_captured_prefix_saved": complete_captured_prefix_saved,
            "saved_evidence_truncated": not complete_captured_prefix_saved,
        },
        "redaction_applied": redaction_applied,
    }


def _combined_diagnostic_preview(stdout: str, stderr: str) -> tuple[str, bool]:
    chunks = [stdout]
    if stdout and stderr:
        chunks.append("\n--- stderr ---\n")
    chunks.append(stderr)
    preview_parts: list[str] = []
    remaining_chars = MAX_DIAGNOSTIC_CHARS
    remaining_bytes = MAX_DIAGNOSTIC_BYTES
    truncated = False
    for index, chunk in enumerate(chunks):
        if not chunk:
            continue
        if remaining_chars <= 0 or remaining_bytes <= 0:
            truncated = True
            break
        selected = chunk[:remaining_chars]
        encoded = selected.encode("utf-8")
        if len(encoded) > remaining_bytes:
            selected = encoded[:remaining_bytes].decode("utf-8", errors="ignore")
            truncated = True
        preview_parts.append(selected)
        used_bytes = len(selected.encode("utf-8"))
        remaining_chars -= len(selected)
        remaining_bytes -= used_bytes
        if len(selected) != len(chunk):
            truncated = True
            break
        if (remaining_chars <= 0 or remaining_bytes <= 0) and any(
            chunks[remaining_index]
            for remaining_index in range(index + 1, len(chunks))
        ):
            truncated = True
            break
    return "".join(preview_parts), truncated


def _stream_evidence(
    observed_stdout: str,
    observed_stderr: str,
    saved_stdout: str,
    saved_stderr: str,
    *,
    stdout_redaction_applied: bool,
    stderr_redaction_applied: bool,
    complete_process_stream_observed: bool = True,
    complete_captured_prefix_saved: bool = True,
    collection_stop_reason: str = "completed",
    output_safety_limit_bytes: int | None = None,
) -> tuple[dict[str, Any], str]:
    preview, preview_truncated = _combined_diagnostic_preview(
        saved_stdout, saved_stderr
    )
    raw_output = saved_stdout or saved_stderr
    raw_source = (
        "saved_redacted_stdout"
        if saved_stdout
        else "saved_redacted_stderr" if saved_stderr else "empty"
    )
    evidence = {
        "stdout": _stream_record(
            observed_stdout,
            saved_stdout,
            redaction_applied=stdout_redaction_applied,
            complete_process_stream_observed=complete_process_stream_observed,
            complete_captured_prefix_saved=complete_captured_prefix_saved,
        ),
        "stderr": _stream_record(
            observed_stderr,
            saved_stderr,
            redaction_applied=stderr_redaction_applied,
            complete_process_stream_observed=complete_process_stream_observed,
            complete_captured_prefix_saved=complete_captured_prefix_saved,
        ),
        "raw_output": {
            "source": raw_source,
            "artifact_paths": ["stdout.txt", "stderr.txt"],
            "artifacts_saved": True,
            "artifact_path": (
                "stdout.txt"
                if saved_stdout
                else "stderr.txt" if saved_stderr else None
            ),
            "duplicate_raw_output_artifact_suppressed": True,
            "complete_captured_prefix_saved": complete_captured_prefix_saved,
            "saved_redacted_evidence": {
                "char_count": len(raw_output),
                "byte_count": len(raw_output.encode("utf-8")),
                "sha256": _text_sha256(raw_output),
                "saved_evidence_truncated": not complete_captured_prefix_saved,
            },
            "contains_unredacted_process_stream": False,
        },
        "diagnostic_preview": {
            "source": "saved_redacted_stdout_and_stderr",
            "saved_preview": {
                "char_count": len(preview),
                "byte_count": len(preview.encode("utf-8")),
                "sha256": _text_sha256(preview),
            },
            "preview_truncated": preview_truncated,
            "complete_saved_evidence_previewed": not preview_truncated,
            "limit_chars": MAX_DIAGNOSTIC_CHARS,
            "limit_bytes": MAX_DIAGNOSTIC_BYTES,
        },
        "captured_process_bytes": len(observed_stdout.encode("utf-8"))
        + len(observed_stderr.encode("utf-8")),
        "complete_process_stream_observed": complete_process_stream_observed,
        "complete_captured_prefix_saved": complete_captured_prefix_saved,
        "saved_evidence_truncated": not complete_captured_prefix_saved,
        "collection_stop_reason": collection_stop_reason,
        "output_safety_limit_exceeded": (
            collection_stop_reason == "output_safety_limit_exceeded"
        ),
    }
    if output_safety_limit_bytes is not None:
        evidence["output_safety_limit_bytes"] = output_safety_limit_bytes
    return evidence, preview


def _safe_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for name in list(environment):
        upper = name.upper()
        if any(part in upper for part in _SECRET_ENV_PARTS):
            environment.pop(name, None)
        elif upper == "TOKEN" or upper.endswith("_TOKEN"):
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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sandbox_snapshot(root: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    boundary_violations: list[str] = []
    scan_errors: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix()
        try:
            resolved = path.resolve(strict=False)
            if path.is_symlink() or not resolved.is_relative_to(root):
                boundary_violations.append(relative)
                continue
            if path.is_file():
                stat = path.stat()
                files[relative] = {
                    "sha256": _sha256(path),
                    "size_bytes": stat.st_size,
                }
        except OSError:
            scan_errors.append(relative)
    return {
        "files": files,
        "boundary_violations": sorted(set(boundary_violations)),
        "scan_errors": sorted(set(scan_errors)),
    }


def _sandbox_file_changes(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    before_paths = set(before)
    after_paths = set(after)
    for path in sorted(after_paths - before_paths):
        changes.append({"change": "created", "path": path, **dict(after[path])})
    for path in sorted(before_paths & after_paths):
        if dict(before[path]) != dict(after[path]):
            changes.append({"change": "modified", "path": path, **dict(after[path])})
    for path in sorted(before_paths - after_paths):
        changes.append({"change": "deleted", "path": path})
    return changes


def _summary(value: Any, *, path_redactions: Sequence[tuple[str, str]] = ()) -> str:
    text = _sanitize(str(value))
    for raw, replacement in path_redactions:
        if raw:
            text = text.replace(raw, replacement)
            text = text.replace(raw.replace("\\", "/"), replacement)
    return text[:512]


def _tool_path(
    value: Any,
    *,
    sandbox_root: Path,
) -> tuple[str, bool]:
    raw = str(value or "").strip()
    if not raw:
        return "", False
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else sandbox_root / candidate).resolve(
        strict=False
    )
    if not resolved.is_relative_to(sandbox_root):
        return "<outside-sandbox>", True
    return resolved.relative_to(sandbox_root).as_posix(), False


def _codex_tool_evidence(
    events: Sequence[Mapping[str, Any]],
    *,
    sandbox_root: Path,
    protected_root: Path | None,
) -> dict[str, Any]:
    exposure_category = find_high_confidence_credential(
        [
            event["item"]
            for event in events
            if isinstance(event.get("item"), Mapping)
            and str(event["item"].get("type") or "") in _CODEX_TOOL_EVENT_TYPES
        ]
    )
    archived: list[dict[str, Any]] = []
    boundary_violations: list[str] = []
    path_redactions = [
        (str(sandbox_root), "<sandbox-root>"),
        (str(Path.home()), "<user-home>"),
    ]
    if protected_root is not None:
        path_redactions.append((str(protected_root), "<protected-root>"))
    for event in events:
        protocol_type = str(event.get("type") or "")
        item = event.get("item")
        if not isinstance(item, Mapping):
            continue
        event_type = str(item.get("type") or "")
        if event_type not in _CODEX_TOOL_EVENT_TYPES:
            continue
        row: dict[str, Any] = {
            "event_type": event_type,
            "protocol_event_type": protocol_type,
            "status": str(item.get("status") or "unknown"),
        }
        if isinstance(item.get("exit_code"), int):
            row["exit_code"] = item["exit_code"]
        if event_type == "command_execution":
            row["command_summary"] = _summary(
                item.get("command") or "", path_redactions=path_redactions
            )
            if item.get("cwd"):
                cwd, outside = _tool_path(item["cwd"], sandbox_root=sandbox_root)
                row["cwd"] = cwd
                if outside:
                    boundary_violations.append("command_execution.cwd")
        elif event_type == "mcp_tool_call":
            row["tool_summary"] = _summary(
                f"{item.get('server') or 'unknown'}/{item.get('tool') or 'unknown'}",
                path_redactions=path_redactions,
            )
        elif event_type == "web_search":
            row["query_summary"] = _summary(
                item.get("query") or "", path_redactions=path_redactions
            )
        elif event_type in {"apply_patch", "file_change"}:
            archived_changes: list[dict[str, str]] = []
            raw_changes = item.get("changes")
            if isinstance(raw_changes, Mapping):
                raw_changes = [
                    {"path": path, "kind": kind}
                    for path, kind in raw_changes.items()
                ]
            if not isinstance(raw_changes, list):
                raw_changes = []
            for change in raw_changes:
                if not isinstance(change, Mapping):
                    continue
                path, outside = _tool_path(
                    change.get("path"), sandbox_root=sandbox_root
                )
                archived_changes.append(
                    {"kind": str(change.get("kind") or "unknown"), "path": path}
                )
                if outside:
                    boundary_violations.append(f"{event_type}.path")
            row["file_changes"] = archived_changes
        archived.append(row)
    counts = Counter(str(row["event_type"]) for row in archived)
    evidence = {
        "tool_events": archived,
        "tool_event_counts": dict(sorted(counts.items())),
        "_sandbox_boundary_violations": sorted(set(boundary_violations)),
    }
    redacted_evidence, redaction_applied = _redact_json_value(evidence)
    if not isinstance(redacted_evidence, dict):
        raise RuntimeError("Tool evidence redaction must preserve the object shape")
    redacted_evidence["redaction_applied_to_tool_evidence"] = bool(
        redaction_applied or exposure_category
    )
    if exposure_category is not None:
        redacted_evidence["_high_confidence_exposure_category"] = exposure_category
    return redacted_evidence


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


def _matches_output_contract(text: str, output_type: str) -> bool:
    if not text.strip():
        return False
    try:
        if output_type == "json":
            extract_json_object(text)
        else:
            extract_file_content(text)
    except ApiError:
        return False
    return True


class _SubscriptionBackend:
    backend_id = ""
    executable = ""
    allow_large_streams = False
    max_process_output_bytes: int | None = None

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
        self.last_stdout: str | None = None
        self.last_stderr: str | None = None
        self.last_diagnostic_preview: str | None = None
        self._ready: dict[tuple[str, str], dict[str, Any]] = {}

    def _command(self, *arguments: str) -> list[str]:
        return [*self.command_prefix, *arguments]

    def _record_large_streams(
        self,
        stdout: str,
        stderr: str,
        *,
        complete_process_stream_observed: bool,
        collection_stop_reason: str,
        output_safety_limit_bytes: int | None = None,
        output_type: str | None = None,
        complete_captured_prefix_saved: bool = True,
    ) -> dict[str, Any]:
        self.last_stdout, stdout_redaction_applied = _redact_codex_stdout(
            stdout, output_type=output_type
        )
        self.last_stderr, stderr_redaction_applied = _redact_text(stderr)
        stream_evidence, self.last_diagnostic_preview = _stream_evidence(
            stdout,
            stderr,
            self.last_stdout,
            self.last_stderr,
            stdout_redaction_applied=stdout_redaction_applied,
            stderr_redaction_applied=stderr_redaction_applied,
            complete_process_stream_observed=complete_process_stream_observed,
            complete_captured_prefix_saved=complete_captured_prefix_saved,
            collection_stop_reason=collection_stop_reason,
            output_safety_limit_bytes=output_safety_limit_bytes,
        )
        self.last_metadata = {
            **self.last_metadata,
            "stream_evidence": stream_evidence,
        }
        return stream_evidence

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        input_text: str | None = None,
        kind: str,
        output_type: str | None = None,
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
                max_output_bytes=self.max_process_output_bytes,
            )
        except ProcessCancelled as exc:
            if self.allow_large_streams:
                self._record_large_streams(
                    exc.stdout,
                    exc.stderr,
                    complete_process_stream_observed=False,
                    collection_stop_reason="cancelled",
                    output_safety_limit_bytes=self.max_process_output_bytes,
                    output_type=output_type,
                    complete_captured_prefix_saved=getattr(
                        exc, "complete_captured_prefix_saved", True
                    ),
                )
                self.last_metadata.update(
                    {
                        "captured_process_bytes": len(exc.stdout.encode("utf-8"))
                        + len(exc.stderr.encode("utf-8")),
                        "complete_process_stream_observed": False,
                        "terminal_state": "cancelled",
                    }
                )
            raise
        except FileNotFoundError as exc:
            raise SubscriptionBackendError(
                "cli_missing",
                f"{self.backend_id} CLI executable was not found",
            ) from exc
        except ProcessOutputLimitExceeded as exc:
            if self.allow_large_streams:
                self._record_large_streams(
                    exc.stdout,
                    exc.stderr,
                    complete_process_stream_observed=False,
                    collection_stop_reason="output_safety_limit_exceeded",
                    output_safety_limit_bytes=exc.limit_bytes,
                    output_type=output_type,
                    complete_captured_prefix_saved=getattr(
                        exc, "complete_captured_prefix_saved", False
                    ),
                )
                self.last_metadata.update(
                    {
                        "captured_process_bytes": exc.captured_bytes,
                        "complete_process_stream_observed": False,
                        "output_safety_limit_bytes": exc.limit_bytes,
                        "terminal_state": "output_safety_limit_exceeded",
                    }
                )
            raise SubscriptionBackendError(
                "output_safety_limit_exceeded",
                f"{self.backend_id} exceeded the process output safety limit",
                raw_output=self.last_diagnostic_preview or "",
                metadata=self.last_metadata,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            if self.allow_large_streams:
                self._record_large_streams(
                    stdout,
                    stderr,
                    complete_process_stream_observed=False,
                    collection_stop_reason="timeout",
                    output_safety_limit_bytes=self.max_process_output_bytes,
                    output_type=output_type,
                    complete_captured_prefix_saved=getattr(
                        exc, "complete_captured_prefix_saved", True
                    ),
                )
                self.last_metadata.update(
                    {
                        "captured_process_bytes": len(stdout.encode("utf-8"))
                        + len(stderr.encode("utf-8")),
                        "complete_process_stream_observed": False,
                        "terminal_state": "timeout",
                    }
                )
            raise SubscriptionBackendError(
                "timeout",
                f"{self.backend_id} timed out after {timeout_seconds} seconds",
                raw_output=(
                    self.last_diagnostic_preview
                    if self.allow_large_streams
                    else f"{stdout}\n{stderr}"
                ),
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
        sandbox_root: Path,
        output_type: str,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    def _execution_evidence(
        self,
        stdout: str,
        *,
        sandbox_root: Path,
    ) -> dict[str, Any]:
        del stdout, sandbox_root
        return {
            "tool_events": [],
            "tool_event_counts": {},
            "_sandbox_boundary_violations": [],
        }

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
        self.last_stdout = None
        self.last_stderr = None
        self.last_diagnostic_preview = None
        report = self.inspect(model=config.model, reasoning_effort=reasoning)
        self.last_stdout = None
        self.last_stderr = None
        self.last_diagnostic_preview = None
        metadata = {
            "backend_id": self.backend_id,
            "cli_version": report["cli_version"],
            "authentication_type": report["authentication_type"],
            "requested_model": config.model,
            "requested_model_catalog_status": report[
                "requested_model_catalog_status"
            ],
            "actual_model": report["actual_model"],
            "actual_model_status": report["actual_model_status"],
            "model_identity_source": report["model_identity_source"],
            "requested_reasoning_effort": reasoning,
            "effective_reasoning_effort": report["effective_reasoning_effort"],
            "tool_execution_policy": report["tool_execution_policy"],
            "sandbox_profile": dict(report["sandbox_profile"]),
        }
        metadata.update({
            field: report[field]
            for field in _SANDBOX_SCOPE_METADATA_FIELDS
            if field in report
        })
        self.last_metadata = dict(metadata)
        before_project = _project_snapshot(self.protected_root, self.protected_target)
        with tempfile.TemporaryDirectory(prefix="windows-lean-loop-agent-") as raw:
            cwd = Path(raw).resolve()
            canary = cwd / "canary.txt"
            canary.touch()
            canary_sha = _sha256(canary)
            mcp_config = cwd / "empty-mcp.json"
            mcp_config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            before_sandbox = _sandbox_snapshot(cwd)

            def sandbox_manifest(
                tool_boundary_violations: Sequence[str] = (),
            ) -> dict[str, Any]:
                after_sandbox = _sandbox_snapshot(cwd)
                protected_state_unchanged = before_project == _project_snapshot(
                    self.protected_root, self.protected_target
                )
                canary_unchanged = (
                    canary.is_file() and _sha256(canary) == canary_sha
                )
                boundary_violations = sorted(
                    set(after_sandbox["boundary_violations"])
                    | set(tool_boundary_violations)
                )
                manifest = {
                    "canary_unchanged": canary_unchanged,
                    "file_changes": _sandbox_file_changes(
                        before_sandbox["files"], after_sandbox["files"]
                    ),
                    "isolation": "repo-external-ephemeral",
                    "network_policy": str(
                        report["sandbox_profile"].get("network_policy") or "unknown"
                    ),
                    "protected_state_unchanged": protected_state_unchanged,
                    "sandbox_boundary_violations": boundary_violations,
                    "scan_errors": list(after_sandbox["scan_errors"]),
                }
                manifest.update({
                    field: report[field]
                    for field in _SANDBOX_SCOPE_METADATA_FIELDS
                    if field in report
                })
                exposure_category = find_high_confidence_credential(manifest)
                redacted_manifest, redaction_applied = _redact_json_value(manifest)
                if not isinstance(redacted_manifest, dict):
                    raise RuntimeError(
                        "Sandbox manifest redaction must preserve the object shape"
                    )
                redacted_manifest["redaction_applied_to_sandbox_manifest"] = bool(
                    redaction_applied or exposure_category
                )
                return redacted_manifest

            def safety_violation(manifest: Mapping[str, Any]) -> str | None:
                if not manifest.get("protected_state_unchanged"):
                    return "side_effect_detected"
                if manifest.get("sandbox_boundary_violations"):
                    return "sandbox_boundary_violation"
                if manifest.get("scan_errors") or not manifest.get("canary_unchanged"):
                    return "sandbox_integrity_violation"
                return None

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
                    output_type=request.output_type,
                )
            except ProcessCancelled:
                manifest = sandbox_manifest()
                terminal = safety_violation(manifest) or "cancelled"
                self.last_metadata = {
                    **self.last_metadata,
                    **metadata,
                    "sandbox_manifest": manifest,
                    "terminal_state": terminal,
                }
                if terminal != "cancelled":
                    raise SubscriptionBackendError(
                        terminal,
                        f"{self.backend_id} violated sandbox safety while cancelling",
                        metadata=self.last_metadata,
                    )
                raise
            except SubscriptionBackendError as exc:
                manifest = sandbox_manifest()
                terminal = safety_violation(manifest)
                if terminal is not None:
                    self.last_metadata = {
                        **exc.metadata,
                        **metadata,
                        "sandbox_manifest": manifest,
                        "terminal_state": terminal,
                    }
                    raise SubscriptionBackendError(
                        terminal,
                        f"{self.backend_id} violated sandbox safety while failing",
                        metadata=self.last_metadata,
                    ) from exc
                exc.metadata.update(metadata)
                exc.metadata["sandbox_manifest"] = manifest
                self.last_metadata = dict(exc.metadata)
                raise
            if self.allow_large_streams:
                stream_evidence = self._record_large_streams(
                    completed.stdout,
                    completed.stderr,
                    complete_process_stream_observed=True,
                    collection_stop_reason="completed",
                    output_safety_limit_bytes=self.max_process_output_bytes,
                    output_type=request.output_type,
                )
                metadata = {
                    **metadata,
                    "stream_evidence": stream_evidence,
                }
            else:
                self.last_stdout = _sanitize(completed.stdout)
                self.last_stderr = _sanitize(completed.stderr)
            execution_evidence = self._execution_evidence(
                completed.stdout,
                sandbox_root=cwd,
            )
            tool_boundary_violations = list(
                execution_evidence.pop("_sandbox_boundary_violations", [])
            )
            execution_exposure_category = execution_evidence.pop(
                "_high_confidence_exposure_category", None
            )
            if execution_exposure_category is not None:
                manifest = sandbox_manifest(tool_boundary_violations)
                self.last_metadata = {
                    **metadata,
                    **execution_evidence,
                    "sandbox_manifest": manifest,
                    "terminal_state": "credential_exposure_detected",
                    "exit_code": completed.returncode,
                }
                raise SubscriptionBackendError(
                    "credential_exposure_detected",
                    (
                        f"{self.backend_id} tool stream contained "
                        "high-confidence credential material"
                    ),
                    raw_output=f"{completed.stdout}\n{completed.stderr}",
                    metadata=self.last_metadata,
                )
            if (
                not self.allow_large_streams
                and (
                    len(completed.stdout) > MAX_DIAGNOSTIC_CHARS
                    or len(completed.stderr) > MAX_DIAGNOSTIC_CHARS
                )
            ):
                manifest = sandbox_manifest(tool_boundary_violations)
                terminal = safety_violation(manifest) or "output_too_large"
                self.last_metadata = {
                    **metadata,
                    **execution_evidence,
                    "sandbox_manifest": manifest,
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
                manifest = sandbox_manifest(tool_boundary_violations)
                kind = safety_violation(manifest) or kind
                self.last_metadata = {
                    **metadata,
                    **execution_evidence,
                    "sandbox_manifest": manifest,
                    "terminal_state": kind,
                    "exit_code": completed.returncode,
                }
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
                    sandbox_root=cwd,
                    output_type=request.output_type,
                )
            except SubscriptionBackendError as exc:
                exc.metadata.update(metadata)
                exc.metadata.update(execution_evidence)
                exc.metadata["exit_code"] = completed.returncode
                exc.metadata["terminal_state"] = exc.kind
                manifest = sandbox_manifest(tool_boundary_violations)
                exc.metadata["sandbox_manifest"] = manifest
                self.last_metadata = dict(exc.metadata)
                terminal = safety_violation(manifest)
                if terminal is not None:
                    self.last_metadata["terminal_state"] = terminal
                    raise SubscriptionBackendError(
                        terminal,
                        f"{self.backend_id} violated sandbox safety while parsing output",
                        metadata=self.last_metadata,
                    ) from exc
                raise
            manifest = sandbox_manifest(tool_boundary_violations)
            terminal = safety_violation(manifest)
            if terminal is not None:
                self.last_metadata = {
                    **metadata,
                    **execution_evidence,
                    **parsed_metadata,
                    "exit_code": completed.returncode,
                    "sandbox_manifest": manifest,
                    "terminal_state": terminal,
                }
                raise SubscriptionBackendError(
                    terminal,
                    f"{self.backend_id} violated sandbox safety",
                    raw_output=f"{completed.stdout}\n{completed.stderr}",
                    metadata=self.last_metadata,
                )
            self.last_metadata = {
                **metadata,
                **execution_evidence,
                **parsed_metadata,
                "exit_code": completed.returncode,
                "output_type": request.output_type,
                "error_classification": None,
                "sandbox_manifest": manifest,
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
    allow_large_streams = True
    max_process_output_bytes = CODEX_MAX_PROCESS_OUTPUT_BYTES

    def _execution_evidence(
        self,
        stdout: str,
        *,
        sandbox_root: Path,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return _codex_tool_evidence(
            events,
            sandbox_root=sandbox_root,
            protected_root=self.protected_root,
        )

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
            "requested_model": model,
            "requested_model_catalog_status": "VALIDATED",
            "actual_model": None,
            "actual_model_status": "NOT_REPORTED_BY_CLIENT",
            "model_identity_source": "REQUESTED_MODEL_AND_OFFICIAL_CATALOG_ONLY",
            "requested_reasoning_effort": reasoning_effort,
            "effective_reasoning_effort": reasoning_effort,
            "supported_reasoning_efforts": sorted(efforts),
            "tool_execution_policy": CODEX_TOOL_EXECUTION_POLICY,
            "sandbox_profile": dict(CODEX_SANDBOX_PROFILE),
            **CODEX_SANDBOX_SCOPE,
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
            "workspace-write",
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "-C",
            str(cwd),
            "-",
        ]

    def _parse(
        self,
        stdout: str,
        *,
        requested_model: str,
        sandbox_root: Path,
        output_type: str,
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
        agent_messages = [
            str(event["item"].get("text") or "")
            for event in events
            if event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "agent_message"
        ]
        if (
            _codex_events_credential_category(events, output_type=output_type)
            is not None
        ):
            raise SubscriptionBackendError(
                "credential_exposure_detected",
                "Codex result stream contained high-confidence credential material",
                raw_output=stdout,
            )
        final_messages = [
            message
            for message in agent_messages
            if _matches_output_contract(message, output_type)
        ]
        thread_started = False
        turn_started = False
        turn_activity = False
        terminal_seen = False
        open_items: dict[str, str] = {}
        completed_items: set[str] = set()
        nonfatal = 0
        protocol_invalid = False
        for event in events:
            event_type = event.get("type")
            if event_type in {"error", "turn.failed", "thread.failed"}:
                protocol_invalid = True
                continue
            if terminal_seen:
                protocol_invalid = True
                continue
            if event_type == "thread.started":
                if (
                    thread_started
                    or turn_started
                    or turn_activity
                    or not str(event.get("thread_id") or "").strip()
                ):
                    protocol_invalid = True
                thread_started = True
                continue
            if event_type == "turn.started":
                if not thread_started or turn_started or turn_activity:
                    protocol_invalid = True
                turn_started = True
                continue
            if event_type in {"item.started", "item.completed"}:
                item = event.get("item")
                if not thread_started or not isinstance(item, dict):
                    protocol_invalid = True
                    continue
                item_id = str(item.get("id") or "")
                item_type = str(item.get("type") or "")
                if not item_id or not item_type:
                    protocol_invalid = True
                    continue
                turn_activity = True
                if event_type == "item.started":
                    if item_id in open_items or item_id in completed_items:
                        protocol_invalid = True
                    else:
                        open_items[item_id] = item_type
                    continue
                if item_id in completed_items:
                    protocol_invalid = True
                    continue
                started_type = open_items.pop(item_id, None)
                if started_type is not None and started_type != item_type:
                    protocol_invalid = True
                completed_items.add(item_id)
                if item_type == "agent_message" and not isinstance(item.get("text"), str):
                    protocol_invalid = True
                if item_type == "error":
                    nonfatal += 1
                continue
            if event_type == "turn.completed":
                if not thread_started or not turn_activity or open_items:
                    protocol_invalid = True
                terminal_seen = True
                continue
            protocol_invalid = True

        completed = sum(event.get("type") == "turn.completed" for event in events)
        final_is_last_agent_message = bool(
            agent_messages
            and len(final_messages) == 1
            and final_messages[0] == agent_messages[-1]
        )
        if (
            protocol_invalid
            or not terminal_seen
            or completed != 1
            or not final_is_last_agent_message
            or not final_messages[0].strip()
        ):
            raise SubscriptionBackendError(
                (
                    "empty_output"
                    if not agent_messages
                    or not any(message.strip() for message in agent_messages)
                    else "output_protocol_incompatible"
                ),
                "Codex did not return one completed final result",
                raw_output=stdout,
            )
        del sandbox_root
        return final_messages[0], {
            "requested_model": requested_model,
            "requested_model_catalog_status": "VALIDATED",
            "actual_model": None,
            "actual_model_status": "NOT_REPORTED_BY_CLIENT",
            "model_identity_source": "REQUESTED_MODEL_AND_OFFICIAL_CATALOG_ONLY",
            "final_result_event": "turn.completed",
            "final_result_selection": "UNIQUE_OUTPUT_CONTRACT_MATCH",
            "agent_message_event_count": len(agent_messages),
            "final_result_candidate_count": len(final_messages),
            "intermediate_agent_message_count": len(agent_messages) - 1,
            "nonfatal_event_count": nonfatal,
            "tool_execution_policy": CODEX_TOOL_EXECUTION_POLICY,
            "sandbox_profile": dict(CODEX_SANDBOX_PROFILE),
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
            "requested_model": model,
            "requested_model_catalog_status": "NOT_AVAILABLE_FROM_CLIENT",
            "actual_model": None,
            "actual_model_status": "NOT_REPORTED_BY_READINESS_PROBE",
            "model_identity_source": "REQUESTED_MODEL_SYNTAX_ONLY",
            "requested_reasoning_effort": reasoning_effort,
            "effective_reasoning_effort": reasoning_effort,
            "supported_reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
            "tool_execution_policy": CLAUDE_TOOL_EXECUTION_POLICY,
            "sandbox_profile": dict(CLAUDE_SANDBOX_PROFILE),
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
        sandbox_root: Path,
        output_type: str,
    ) -> tuple[str, dict[str, Any]]:
        del sandbox_root, output_type
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
        if not isinstance(usage, dict) or requested_model not in usage:
            raise SubscriptionBackendError(
                "model_identity_required",
                "Claude did not verify the requested model in modelUsage",
                raw_output=stdout,
            )
        return result, {
            "requested_model": requested_model,
            "requested_model_catalog_status": "NOT_AVAILABLE_FROM_CLIENT",
            "actual_model": requested_model,
            "actual_model_status": "REPORTED_BY_CLIENT",
            "model_identity_source": "CLIENT_RESULT_MODEL_USAGE",
            "final_result_event": "result:success:completed",
            "nonfatal_event_count": 0,
            "tool_event_counts": {},
            "tool_events": [],
            "tool_execution_policy": CLAUDE_TOOL_EXECUTION_POLICY,
            "sandbox_profile": dict(CLAUDE_SANDBOX_PROFILE),
        }


def build_subscription_identity_summary(
    backend: _SubscriptionBackend,
    configurations: Mapping[str, ApiConfig],
) -> dict[str, Any]:
    requests: dict[str, dict[str, Any]] = {}
    common: dict[str, Any] | None = None
    common_fields = (
        "backend_id",
        "cli_version",
        "authentication_type",
        "tool_execution_policy",
        "sandbox_profile",
    )
    request_fields = (
        "requested_model",
        "requested_model_catalog_status",
        "actual_model",
        "actual_model_status",
        "model_identity_source",
        "requested_reasoning_effort",
        "effective_reasoning_effort",
    )
    for role, config in configurations.items():
        report = backend.inspect(
            model=config.model,
            reasoning_effort=str(config.reasoning_effort or ""),
        )
        candidate_common = {field: report[field] for field in common_fields}
        candidate_common.update({
            field: report[field]
            for field in _SANDBOX_SCOPE_METADATA_FIELDS
            if field in report
        })
        if common is None:
            common = candidate_common
        elif common != candidate_common:
            raise SubscriptionBackendError(
                "backend_identity_inconsistent",
                "Subscription backend readiness identity changed across workflow roles",
            )
        requests[role] = {field: report[field] for field in request_fields}
    return {**dict(common or {}), "requests": requests}


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
