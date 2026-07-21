from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lean_loop.project_config import (
    load_project_api_key,
    load_project_config,
    load_provider_api_key,
    load_provider_profiles,
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ApiConfig:
    api_base: str
    api_key: str
    model: str
    mode: str
    timeout_seconds: int
    curl_executable: str
    reasoning_effort: str | None = None
    disable_response_storage: bool = True
    max_output_tokens: int = 8192
    empty_response_retries: int = 1
    api_timeout_retries: int = 1
    stream_responses: bool = True
    provider_kind: str = "openai-compatible"
    provider_id: str = "default"

    @property
    def endpoint(self) -> str:
        suffix = "responses" if self.mode == "responses" else "chat/completions"
        return f"{self.api_base.rstrip('/')}/{suffix}"

    @classmethod
    def from_environment(
        cls,
        project: Path | None = None,
        provider_id: str = "default",
    ) -> "ApiConfig":
        default_values = load_project_config(project) if project is not None else {}
        if provider_id == "default":
            project_values = default_values
        else:
            profiles = load_provider_profiles(project) if project is not None else {}
            if provider_id not in profiles:
                raise ConfigError(f"Unknown provider profile: {provider_id}")
            project_values = {**default_values, **profiles[provider_id]}
        api_base = str(
            project_values.get("api_base")
            or os.environ.get("LEAN_AGENT_API_BASE", "")
        ).strip()
        environment_key = (
            os.environ.get("LEAN_AGENT_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
        project_key = (
            load_project_api_key(project)
            if project is not None and provider_id == "default"
            else load_provider_api_key(project, provider_id)
            if project is not None
            else ""
        )
        api_key = project_key or (environment_key if provider_id == "default" else "")
        model = str(
            project_values.get("model") or os.environ.get("LEAN_AGENT_MODEL", "")
        ).strip()
        mode = str(
            project_values.get("api_mode")
            or os.environ.get("LEAN_AGENT_API_MODE", "responses")
        ).strip().lower()
        curl_executable = os.environ.get("LEAN_AGENT_CURL", "curl.exe").strip()
        reasoning_effort = str(
            project_values.get("reasoning_effort")
            or os.environ.get("LEAN_AGENT_REASONING_EFFORT", "")
        ).strip()
        disable_storage_value = project_values.get("disable_response_storage")
        disable_storage_raw = (
            str(disable_storage_value).lower()
            if disable_storage_value is not None
            else os.environ.get("LEAN_AGENT_DISABLE_RESPONSE_STORAGE", "true").strip().lower()
        )

        missing = [
            name
            for name, value in (
                ("LEAN_AGENT_API_BASE", api_base),
                ("LEAN_AGENT_API_KEY or OPENAI_API_KEY", api_key),
                ("LEAN_AGENT_MODEL", model),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"Missing environment variable(s): {', '.join(missing)}")
        if mode not in {"responses", "chat-completions"}:
            raise ConfigError(
                "LEAN_AGENT_API_MODE must be 'responses' or 'chat-completions'"
            )
        try:
            timeout_seconds = int(
                project_values.get(
                    "timeout_seconds",
                    os.environ.get("LEAN_AGENT_TIMEOUT_SECONDS", "180"),
                )
            )
        except ValueError as exc:
            raise ConfigError("LEAN_AGENT_TIMEOUT_SECONDS must be an integer") from exc
        if timeout_seconds < 1:
            raise ConfigError("LEAN_AGENT_TIMEOUT_SECONDS must be positive")
        try:
            max_output_tokens = int(
                project_values.get(
                    "max_output_tokens",
                    os.environ.get("LEAN_AGENT_MAX_OUTPUT_TOKENS", "8192"),
                )
            )
            empty_response_retries = int(
                os.environ.get("LEAN_AGENT_EMPTY_RESPONSE_RETRIES", "1")
            )
            api_timeout_retries = int(
                project_values.get(
                    "api_timeout_retries",
                    os.environ.get("LEAN_AGENT_API_TIMEOUT_RETRIES", "1"),
                )
            )
        except ValueError as exc:
            raise ConfigError(
                "LEAN_AGENT_MAX_OUTPUT_TOKENS, LEAN_AGENT_EMPTY_RESPONSE_RETRIES, "
                "and LEAN_AGENT_API_TIMEOUT_RETRIES must be integers"
            ) from exc
        if max_output_tokens < 256:
            raise ConfigError("LEAN_AGENT_MAX_OUTPUT_TOKENS must be at least 256")
        if empty_response_retries < 0:
            raise ConfigError("LEAN_AGENT_EMPTY_RESPONSE_RETRIES cannot be negative")
        if api_timeout_retries < 0:
            raise ConfigError("LEAN_AGENT_API_TIMEOUT_RETRIES cannot be negative")
        if disable_storage_raw not in {"true", "false", "1", "0", "yes", "no"}:
            raise ConfigError(
                "LEAN_AGENT_DISABLE_RESPONSE_STORAGE must be true or false"
            )
        disable_response_storage = disable_storage_raw in {"true", "1", "yes"}
        stream_value = project_values.get("stream_responses")
        stream_responses = (
            bool(stream_value)
            if stream_value is not None
            else os.environ.get("LEAN_AGENT_STREAM_RESPONSES", "true").strip().lower()
            in {"true", "1", "yes"}
        )
        provider_kind = str(
            project_values.get("provider_kind") or "openai-compatible"
        ).strip()
        if provider_kind not in {"openai-compatible", "deepseek"}:
            raise ConfigError(f"Unsupported provider kind: {provider_kind}")

        return cls(
            api_base=api_base,
            api_key=api_key,
            model=model,
            mode=mode,
            timeout_seconds=timeout_seconds,
            curl_executable=curl_executable,
            reasoning_effort=reasoning_effort or None,
            disable_response_storage=disable_response_storage,
            max_output_tokens=max_output_tokens,
            empty_response_retries=empty_response_retries,
            api_timeout_retries=api_timeout_retries,
            stream_responses=stream_responses,
            provider_kind=provider_kind,
            provider_id=provider_id,
        )

    @classmethod
    def for_backend(
        cls,
        project: Path | None,
        backend_id: str = "direct",
        *,
        provider_id: str = "default",
        model: str = "",
        reasoning_effort: str | None = None,
    ) -> "ApiConfig":
        if backend_id == "direct":
            config = cls.from_environment(project, provider_id)
            return cls(
                **{
                    **config.__dict__,
                    "model": model.strip() or config.model,
                    "reasoning_effort": (
                        reasoning_effort
                        if reasoning_effort is not None
                        else config.reasoning_effort
                    ),
                }
            )
        if backend_id not in {"codex-subscription", "claude-subscription"}:
            raise ConfigError(f"Unsupported Agent backend: {backend_id}")
        if provider_id != "default":
            raise ConfigError("Subscription backends do not accept API provider profiles")
        values = load_project_config(project) if project is not None else {}
        selected_model = str(
            model or values.get("model") or os.environ.get("LEAN_AGENT_MODEL", "")
        ).strip()
        if not selected_model:
            raise ConfigError("Subscription backends require an explicit model")
        try:
            timeout_seconds = int(
                values.get(
                    "timeout_seconds",
                    os.environ.get("LEAN_AGENT_TIMEOUT_SECONDS", "180"),
                )
            )
        except ValueError as exc:
            raise ConfigError("LEAN_AGENT_TIMEOUT_SECONDS must be an integer") from exc
        if timeout_seconds < 1:
            raise ConfigError("LEAN_AGENT_TIMEOUT_SECONDS must be positive")
        selected_reasoning = (
            reasoning_effort
            if reasoning_effort is not None
            else str(
                values.get("reasoning_effort")
                or os.environ.get("LEAN_AGENT_REASONING_EFFORT", "")
            ).strip()
            or None
        )
        return cls(
            api_base="",
            api_key="",
            model=selected_model,
            mode="subscription",
            timeout_seconds=timeout_seconds,
            curl_executable="",
            reasoning_effort=selected_reasoning,
            empty_response_retries=0,
            api_timeout_retries=0,
            stream_responses=True,
            provider_kind=backend_id,
            provider_id="subscription",
        )
