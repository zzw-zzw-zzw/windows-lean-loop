from __future__ import annotations

import base64
import ctypes
import json
import os
import re
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lean_loop.jsonutil import atomic_write_json, read_json


CONFIG_KEYS = {
    "api_base",
    "model",
    "api_mode",
    "api_transport",
    "reasoning_effort",
    "disable_response_storage",
    "lake",
    "timeout_seconds",
    "max_output_tokens",
    "api_timeout_retries",
    "stream_responses",
    "provider_kind",
    "lsp_mode",
    "lsp_command",
    "lsp_url",
    "lsp_startup_timeout_seconds",
    "lsp_call_timeout_seconds",
    "lsp_remote_search",
    "lsp_max_search_terms",
}
EFFORTS = {"low", "medium", "high", "xhigh"}
PROVIDER_KINDS = {"openai-compatible", "deepseek"}
_PROVIDER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")


class ProjectConfigError(ValueError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    )
    return value, buffer


def _dpapi_encrypt(value: str) -> str:
    if os.name != "nt":
        raise ProjectConfigError("Persistent API keys require Windows DPAPI")
    source, source_buffer = _blob(value.encode("utf-8"))
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "Lean Agent API Key",
        None,
        None,
        None,
        0,
        ctypes.byref(target),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
    del source_buffer
    return base64.b64encode(encrypted).decode("ascii")


def _dpapi_decrypt(value: str) -> str:
    if os.name != "nt":
        raise ProjectConfigError("Persistent API keys require Windows DPAPI")
    try:
        encrypted = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProjectConfigError("Stored API key is not valid DPAPI data") from exc
    source, source_buffer = _blob(encrypted)
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ProjectConfigError(
            "Stored API key cannot be decrypted by the current Windows user"
        )
    try:
        decrypted = ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(target.pbData)
    del source_buffer
    return decrypted


def _paths(project: Path) -> tuple[Path, Path]:
    root = project / ".lean-agent"
    return root / "config.json", root / "secrets.json"


def _raw_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = read_json(path)
    if not isinstance(value, dict):
        raise ProjectConfigError(f"Configuration must be a JSON object: {path}")
    return value


def load_project_config(project: Path) -> dict[str, Any]:
    config_path, _ = _paths(project)
    if not config_path.is_file():
        return {}
    try:
        value = _raw_json(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectConfigError(f"Invalid project configuration: {exc}") from exc
    return {key: value[key] for key in CONFIG_KEYS if key in value}


def load_project_api_key(project: Path) -> str:
    _, secret_path = _paths(project)
    if not secret_path.is_file():
        return ""
    try:
        value = _raw_json(secret_path)
        provider_keys = value.get("provider_api_keys_dpapi", {})
        encrypted = (
            provider_keys.get("default", "")
            if isinstance(provider_keys, dict)
            else ""
        ) or value.get("api_key_dpapi", "")
        return _dpapi_decrypt(str(encrypted)) if encrypted else ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectConfigError(f"Invalid project secret: {exc}") from exc


def _validated_config(value: dict[str, Any]) -> dict[str, Any]:
    unknown = set(value) - CONFIG_KEYS
    if unknown:
        raise ProjectConfigError(
            f"Unsupported configuration fields: {', '.join(sorted(unknown))}"
        )
    result: dict[str, Any] = {}
    for key in (
        "api_base", "model", "api_mode", "api_transport", "reasoning_effort", "lake", "provider_kind",
        "lsp_mode", "lsp_command", "lsp_url",
    ):
        if key in value:
            result[key] = str(value[key]).strip()
    if result.get("api_base"):
        parsed = urlparse(result["api_base"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProjectConfigError("API base must be an http:// or https:// URL")
    if result.get("api_mode") and result["api_mode"] not in {
        "responses",
        "chat-completions",
    }:
        raise ProjectConfigError("API mode must be responses or chat-completions")
    if result.get("api_transport") and result["api_transport"] not in {
        "auto",
        "python",
        "curl",
    }:
        raise ProjectConfigError("API transport must be auto, python, or curl")
    if result.get("reasoning_effort") and result["reasoning_effort"] not in EFFORTS:
        raise ProjectConfigError("Reasoning effort must be low, medium, high, or xhigh")
    if result.get("provider_kind") and result["provider_kind"] not in PROVIDER_KINDS:
        raise ProjectConfigError(
            "Provider kind must be openai-compatible or deepseek"
        )
    if result.get("lsp_mode") and result["lsp_mode"] not in {
        "off",
        "stdio",
        "http",
    }:
        raise ProjectConfigError("LSP mode must be off, stdio, or http")
    if result.get("lsp_url"):
        parsed = urlparse(result["lsp_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProjectConfigError("LSP URL must be an http:// or https:// URL")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ProjectConfigError(
                "HTTP LSP MCP is restricted to loopback addresses"
            )
    for key, minimum in (
        ("timeout_seconds", 1),
        ("max_output_tokens", 256),
        ("api_timeout_retries", 0),
        ("lsp_startup_timeout_seconds", 1),
        ("lsp_call_timeout_seconds", 1),
        ("lsp_max_search_terms", 1),
    ):
        if key in value:
            try:
                result[key] = int(value[key])
            except (TypeError, ValueError) as exc:
                raise ProjectConfigError(f"{key} must be an integer") from exc
            if result[key] < minimum:
                raise ProjectConfigError(f"{key} must be at least {minimum}")
    if result.get("lsp_max_search_terms", 1) > 10:
        raise ProjectConfigError("lsp_max_search_terms must be at most 10")
    for key in (
        "disable_response_storage",
        "stream_responses",
        "lsp_remote_search",
    ):
        if key in value:
            result[key] = bool(value[key])
    return result


def save_project_config(
    project: Path,
    value: dict[str, Any],
    *,
    api_key: str | None = None,
    clear_api_key: bool = False,
) -> dict[str, Any]:
    config_path, secret_path = _paths(project)
    validated = _validated_config(value)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _raw_json(config_path)
    providers = existing.get("providers", {})
    atomic_write_json(
        config_path,
        {
            "schema_version": 2,
            **validated,
            "providers": providers if isinstance(providers, dict) else {},
        },
    )
    secret_value = _raw_json(secret_path)
    provider_keys = secret_value.get("provider_api_keys_dpapi", {})
    if not isinstance(provider_keys, dict):
        provider_keys = {}
    if clear_api_key:
        provider_keys.pop("default", None)
        secret_value.pop("api_key_dpapi", None)
    elif api_key is not None and api_key.strip():
        provider_keys["default"] = _dpapi_encrypt(api_key.strip())
    if provider_keys or secret_value.get("api_key_dpapi"):
        atomic_write_json(
            secret_path,
            {
                "schema_version": 2,
                **({"api_key_dpapi": secret_value["api_key_dpapi"]} if secret_value.get("api_key_dpapi") else {}),
                "provider_api_keys_dpapi": provider_keys,
            },
        )
    elif secret_path.is_file():
        secret_path.unlink()
    return project_config_view(project)


def load_provider_profiles(project: Path) -> dict[str, dict[str, Any]]:
    config_path, _ = _paths(project)
    try:
        raw = _raw_json(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectConfigError(f"Invalid project configuration: {exc}") from exc
    providers = raw.get("providers", {})
    if not isinstance(providers, dict):
        raise ProjectConfigError("providers must be an object")
    result: dict[str, dict[str, Any]] = {}
    for provider_id, value in providers.items():
        if not _PROVIDER_ID_RE.fullmatch(str(provider_id)) or not isinstance(value, dict):
            continue
        result[str(provider_id)] = _validated_config(value)
    return result


def load_provider_api_key(project: Path, provider_id: str) -> str:
    if provider_id == "default":
        return load_project_api_key(project)
    _, secret_path = _paths(project)
    if not secret_path.is_file():
        return ""
    try:
        raw = _raw_json(secret_path)
        provider_keys = raw.get("provider_api_keys_dpapi", {})
        encrypted = provider_keys.get(provider_id, "") if isinstance(provider_keys, dict) else ""
        return _dpapi_decrypt(str(encrypted)) if encrypted else ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectConfigError(f"Invalid project secret: {exc}") from exc


def save_provider_profile(
    project: Path,
    provider_id: str,
    value: dict[str, Any],
    *,
    api_key: str | None = None,
    clear_api_key: bool = False,
) -> dict[str, Any]:
    provider_id = provider_id.strip()
    if provider_id == "default":
        return save_project_config(
            project,
            value,
            api_key=api_key,
            clear_api_key=clear_api_key,
        )
    if not _PROVIDER_ID_RE.fullmatch(provider_id):
        raise ProjectConfigError(
            "Provider ID must use 1-32 letters, numbers, underscores, or hyphens"
        )
    config_path, secret_path = _paths(project)
    raw = _raw_json(config_path)
    providers = raw.get("providers", {})
    if not isinstance(providers, dict):
        providers = {}
    validated = _validated_config(value)
    if not validated.get("api_base") or not validated.get("model"):
        raise ProjectConfigError("Provider API base and model are required")
    if (
        validated.get("provider_kind") == "deepseek"
        and validated.get("api_mode") != "chat-completions"
    ):
        raise ProjectConfigError(
            "DeepSeek Official provider must use chat-completions mode"
        )
    providers[provider_id] = validated
    default_values = {key: raw[key] for key in CONFIG_KEYS if key in raw}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        config_path,
        {"schema_version": 2, **default_values, "providers": providers},
    )

    secret_value = _raw_json(secret_path)
    provider_keys = secret_value.get("provider_api_keys_dpapi", {})
    if not isinstance(provider_keys, dict):
        provider_keys = {}
    if clear_api_key:
        provider_keys.pop(provider_id, None)
    elif api_key is not None and api_key.strip():
        provider_keys[provider_id] = _dpapi_encrypt(api_key.strip())
    atomic_write_json(
        secret_path,
        {
            "schema_version": 2,
            **({"api_key_dpapi": secret_value["api_key_dpapi"]} if secret_value.get("api_key_dpapi") else {}),
            "provider_api_keys_dpapi": provider_keys,
        },
    )
    return provider_profiles_view(project)


def provider_profiles_view(project: Path) -> dict[str, Any]:
    profiles = {"default": project_config_view(project)}
    for provider_id, value in load_provider_profiles(project).items():
        profiles[provider_id] = {
            **value,
            "api_key_configured": bool(load_provider_api_key(project, provider_id)),
            "api_key_source": "project" if load_provider_api_key(project, provider_id) else "missing",
            "stored": True,
        }
    return profiles


def project_config_view(project: Path) -> dict[str, Any]:
    stored = load_project_config(project)
    persisted_key = False
    try:
        persisted_key = bool(load_project_api_key(project))
    except ProjectConfigError:
        persisted_key = False
    environment_key = bool(
        os.environ.get("LEAN_AGENT_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    defaults = {
        "api_base": os.environ.get("LEAN_AGENT_API_BASE", "").strip(),
        "model": os.environ.get("LEAN_AGENT_MODEL", "").strip(),
        "api_mode": os.environ.get("LEAN_AGENT_API_MODE", "responses").strip(),
        "api_transport": os.environ.get(
            "LEAN_AGENT_API_TRANSPORT", "auto"
        ).strip().lower(),
        "reasoning_effort": os.environ.get(
            "LEAN_AGENT_REASONING_EFFORT", "medium"
        ).strip(),
        "disable_response_storage": os.environ.get(
            "LEAN_AGENT_DISABLE_RESPONSE_STORAGE", "true"
        ).strip().lower()
        in {"true", "1", "yes"},
        "lake": os.environ.get("LEAN_AGENT_LAKE", "lake").strip(),
        "timeout_seconds": int(os.environ.get("LEAN_AGENT_TIMEOUT_SECONDS", "180")),
        "max_output_tokens": int(
            os.environ.get("LEAN_AGENT_MAX_OUTPUT_TOKENS", "8192")
        ),
        "api_timeout_retries": int(
            os.environ.get("LEAN_AGENT_API_TIMEOUT_RETRIES", "1")
        ),
        "stream_responses": os.environ.get(
            "LEAN_AGENT_STREAM_RESPONSES", "true"
        ).strip().lower()
        in {"true", "1", "yes"},
        "provider_kind": "openai-compatible",
        "lsp_mode": os.environ.get("LEAN_AGENT_LSP_MODE", "off").strip().lower(),
        "lsp_command": os.environ.get(
            "LEAN_AGENT_LSP_COMMAND", "lean-lsp-mcp"
        ).strip(),
        "lsp_url": os.environ.get(
            "LEAN_AGENT_LSP_URL", "http://127.0.0.1:8000/mcp"
        ).strip(),
        "lsp_startup_timeout_seconds": int(
            os.environ.get("LEAN_AGENT_LSP_STARTUP_TIMEOUT", "180")
        ),
        "lsp_call_timeout_seconds": int(
            os.environ.get("LEAN_AGENT_LSP_CALL_TIMEOUT", "60")
        ),
        "lsp_remote_search": os.environ.get(
            "LEAN_AGENT_LSP_REMOTE_SEARCH", "true"
        ).strip().lower()
        in {"true", "1", "yes"},
        "lsp_max_search_terms": int(
            os.environ.get("LEAN_AGENT_LSP_MAX_SEARCH_TERMS", "3")
        ),
    }
    effective = {**defaults, **stored}
    return {
        **effective,
        "api_key_configured": persisted_key or environment_key,
        "api_key_source": "project" if persisted_key else "environment" if environment_key else "missing",
        "stored": bool(stored),
        "providers": sorted(load_provider_profiles(project)),
    }
