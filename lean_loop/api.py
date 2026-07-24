from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from lean_loop.config import ApiConfig
from lean_loop.process_control import ProcessControl, run_controlled_process
from lean_loop.prompts import PROVER_SYSTEM_PROMPT


class ApiError(RuntimeError):
    pass


class ApiTimeoutError(ApiError):
    pass


class TransientApiError(ApiError):
    pass


class MissingFinalOutputError(ApiError):
    pass


class MalformedModelOutputError(ApiError):
    def __init__(self, message: str, raw_output: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def _notify_progress(
    control: ProcessControl | None, event: str, **details: Any
) -> None:
    callback = getattr(control, "process_progress", None)
    if callable(callback):
        callback("api", {"event": event, **details})


class _ResponsesStream:
    def __init__(self, control: ProcessControl | None) -> None:
        self.control = control
        self.event_name = ""
        self.data_lines: list[str] = []
        self.output_parts: list[str] = []
        self.completed_response: dict[str, Any] | None = None
        self.event_count = 0
        self.reasoning_events = 0
        self.output_characters = 0

    def feed_line(self, line: str) -> None:
        value = line.rstrip("\r\n")
        if not value:
            self._dispatch()
        elif value.startswith("event:"):
            self.event_name = value.removeprefix("event:").strip()
        elif value.startswith("data:"):
            self.data_lines.append(value.removeprefix("data:").lstrip())

    def _dispatch(self) -> None:
        if not self.data_lines:
            self.event_name = ""
            return
        raw = "\n".join(self.data_lines)
        self.data_lines = []
        if raw == "[DONE]":
            _notify_progress(self.control, "response.done")
            self.event_name = ""
            return
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            self.event_name = ""
            return
        if not isinstance(value, dict):
            self.event_name = ""
            return
        event = str(value.get("type") or self.event_name or "response.event")
        self.event_count += 1
        if event == "response.output_text.delta":
            delta = value.get("delta")
            if isinstance(delta, str):
                self.output_parts.append(delta)
                self.output_characters += len(delta)
            _notify_progress(
                self.control,
                event,
                events=self.event_count,
                output_characters=self.output_characters,
            )
        elif "reasoning" in event:
            self.reasoning_events += 1
            _notify_progress(
                self.control,
                event,
                events=self.event_count,
                reasoning_events=self.reasoning_events,
            )
        elif event == "response.completed":
            response = value.get("response")
            if isinstance(response, dict):
                self.completed_response = response
            _notify_progress(
                self.control,
                event,
                events=self.event_count,
                output_characters=self.output_characters,
            )
        elif event in {"response.failed", "response.incomplete", "error"}:
            _notify_progress(self.control, event, events=self.event_count)
        else:
            _notify_progress(self.control, event, events=self.event_count)
        self.event_name = ""

    def final_text(self, raw_output: str) -> str:
        self._dispatch()
        if self.output_parts:
            return "".join(self.output_parts)
        if self.completed_response is not None:
            return extract_response_text(self.completed_response, "responses")
        try:
            value = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise MissingFinalOutputError(
                "Responses stream ended without final output"
            ) from exc
        if not isinstance(value, dict):
            raise MissingFinalOutputError(
                "Responses stream returned an invalid final object"
            )
        return extract_response_text(value, "responses")


def _request_payload(
    config: ApiConfig,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    if config.mode == "responses":
        payload: dict[str, Any] = {
            "model": config.model,
            "store": not config.disable_response_storage,
            "max_output_tokens": config.max_output_tokens,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt}],
                },
            ],
        }
        if config.reasoning_effort:
            payload["reasoning"] = {"effort": config.reasoning_effort}
        if config.stream_responses:
            payload["stream"] = True
        return payload
    if config.provider_kind == "deepseek":
        return {
            "model": config.model,
            "max_tokens": config.max_output_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    payload = {
        "model": config.model,
        "store": not config.disable_response_storage,
        "max_completion_tokens": config.max_output_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if config.reasoning_effort:
        payload["reasoning_effort"] = config.reasoning_effort
    return payload


def _curl_config(
    endpoint: str,
    api_key: str,
    payload_path: Path,
    timeout_seconds: int,
) -> str:
    payload = payload_path.resolve().as_posix()
    escaped_endpoint = endpoint.replace('"', '\\"')
    escaped_key = api_key.replace('"', '\\"')
    return "\n".join(
        [
            f'url = "{escaped_endpoint}"',
            'request = "POST"',
            f'header = "Authorization: Bearer {escaped_key}"',
            'header = "Content-Type: application/json"',
            f'data-binary = "@{payload}"',
            "connect-timeout = 15",
            f"max-time = {timeout_seconds}",
            "no-buffer",
            "silent",
            "show-error",
            "fail-with-body",
        ]
    )


def effective_api_transport(config: ApiConfig) -> str:
    if config.api_transport != "auto":
        return config.api_transport
    return "python" if os.name == "nt" else "curl"


def _python_transport_input(
    endpoint: str,
    api_key: str,
    payload_path: Path,
    timeout_seconds: int,
) -> str:
    return json.dumps(
        {
            "endpoint": endpoint,
            "api_key": api_key,
            "payload_path": str(payload_path.resolve()),
            "timeout_seconds": timeout_seconds,
        },
        ensure_ascii=False,
    )


def _call_model_text_once(
    config: ApiConfig,
    system_prompt: str,
    user_prompt: str,
    temp_dir: Path,
    process_control: ProcessControl | None = None,
) -> str:
    temp_dir.mkdir(parents=True, exist_ok=True)
    payload = _request_payload(config, system_prompt, user_prompt)
    stream = _ResponsesStream(process_control) if config.mode == "responses" and config.stream_responses else None

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="request-",
        dir=temp_dir,
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        payload_path = Path(handle.name)

    transport = effective_api_transport(config)
    if transport == "python":
        command = [sys.executable, "-m", "lean_loop.python_transport"]
        input_text = _python_transport_input(
            config.endpoint,
            config.api_key,
            payload_path,
            config.timeout_seconds,
        )
    else:
        command = [config.curl_executable, "--config", "-"]
        input_text = _curl_config(
            config.endpoint,
            config.api_key,
            payload_path,
            config.timeout_seconds,
        )
    _notify_progress(process_control, "transport.selected", transport=transport)
    try:
        completed = run_controlled_process(
            command,
            input_text=input_text,
            timeout_seconds=config.timeout_seconds + 5,
            kind="api",
            control=process_control,
            stdout_line_callback=stream.feed_line if stream is not None else None,
        )
    except FileNotFoundError as exc:
        executable = config.curl_executable if transport == "curl" else sys.executable
        raise ApiError(f"API transport executable not found: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ApiTimeoutError(
            f"API request timed out after {config.timeout_seconds}s"
        ) from exc
    finally:
        payload_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = "\n".join(
            value.strip()
            for value in (completed.stdout, completed.stderr)
            if value and value.strip()
        )
        if completed.returncode == 28:
            raise ApiTimeoutError(
                f"API request timed out after {config.timeout_seconds}s: {detail}"
            )
        if completed.returncode == 22 and re.search(
            r"\b(?:429|500|502|503|504)\b|Bad Gateway|Service Unavailable|Gateway Timeout|Too Many Requests",
            detail,
            re.IGNORECASE,
        ):
            raise TransientApiError(
                f"Transient API gateway failure ({transport} exit {completed.returncode}): {detail}"
            )
        raise ApiError(
            f"API request failed ({transport} exit {completed.returncode}): {detail}"
        )

    if stream is not None:
        return stream.final_text(completed.stdout)

    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        preview = completed.stdout[:500].strip()
        raise ApiError(f"API returned invalid JSON: {preview}") from exc

    return extract_response_text(response, config.mode)


def _lower_reasoning_effort(effort: str | None) -> str | None:
    order = {"xhigh": "high", "high": "medium", "medium": "low", "low": "low"}
    return order.get(effort or "", effort)


def call_model_text(
    config: ApiConfig,
    system_prompt: str,
    user_prompt: str,
    temp_dir: Path,
    process_control: ProcessControl | None = None,
) -> str:
    active_config = config
    active_prompt = user_prompt
    empty_retries = 0
    timeout_retries = 0
    transient_retries = 0
    while True:
        try:
            return _call_model_text_once(
                active_config,
                system_prompt,
                active_prompt,
                temp_dir,
                process_control,
            )
        except ApiTimeoutError:
            if timeout_retries >= config.api_timeout_retries:
                raise
            timeout_retries += 1
            lowered = _lower_reasoning_effort(active_config.reasoning_effort)
            _notify_progress(
                process_control,
                "response.retry",
                reason="timeout",
                retry=timeout_retries,
                retries=config.api_timeout_retries,
                reasoning_effort=lowered,
            )
            active_config = replace(
                active_config,
                reasoning_effort=lowered,
                api_timeout_retries=0,
            )
            active_prompt = (
                user_prompt
                + "\n\nThe previous request timed out before final output. "
                "Use a more direct proof strategy and return the required JSON promptly."
            )
        except TransientApiError:
            if transient_retries >= config.api_timeout_retries:
                raise
            transient_retries += 1
            _notify_progress(
                process_control,
                "response.retry",
                reason="transient-http",
                retry=transient_retries,
                retries=config.api_timeout_retries,
                reasoning_effort=active_config.reasoning_effort,
            )
            active_config = replace(active_config, api_timeout_retries=0)
        except MissingFinalOutputError:
            if empty_retries >= config.empty_response_retries:
                raise
            empty_retries += 1
            active_config = replace(
                active_config,
                reasoning_effort=_lower_reasoning_effort(
                    active_config.reasoning_effort
                ),
                empty_response_retries=0,
            )
            active_prompt = (
                user_prompt
                + "\n\nThe previous API response contained reasoning but no final text. "
                "Return the required final JSON object now. Keep reasoning concise."
            )


def call_model(
    config: ApiConfig,
    user_prompt: str,
    temp_dir: Path,
    *,
    process_control: ProcessControl | None = None,
) -> str:
    active_prompt = user_prompt
    retries = max(1, config.empty_response_retries)
    for retry in range(retries + 1):
        text = call_model_text(
            config,
            PROVER_SYSTEM_PROMPT,
            active_prompt,
            temp_dir,
            process_control,
        )
        try:
            return extract_file_content(text)
        except MalformedModelOutputError:
            if retry >= retries:
                raise
            _notify_progress(
                process_control,
                "response.retry",
                reason="malformed-structured-output",
                retry=retry + 1,
                retries=retries,
                reasoning_effort=_lower_reasoning_effort(config.reasoning_effort),
            )
            active_prompt = (
                user_prompt
                + "\n\nYour previous final answer could not be parsed. "
                "Return only one valid JSON object with a single string field "
                'named "content". Escape all newlines and quotes correctly. '
                "Do not use Markdown or explanatory text."
            )
    raise AssertionError("unreachable")


def call_model_json(
    config: ApiConfig,
    system_prompt: str,
    user_prompt: str,
    temp_dir: Path,
    *,
    process_control: ProcessControl | None = None,
) -> dict[str, Any]:
    active_prompt = user_prompt
    retries = max(1, config.empty_response_retries)
    for retry in range(retries + 1):
        text = call_model_text(
            config,
            system_prompt,
            active_prompt,
            temp_dir,
            process_control,
        )
        try:
            return extract_json_object(text)
        except MalformedModelOutputError:
            if retry >= retries:
                raise
            _notify_progress(
                process_control,
                "response.retry",
                reason="malformed-structured-output",
                retry=retry + 1,
                retries=retries,
                reasoning_effort=_lower_reasoning_effort(config.reasoning_effort),
            )
            active_prompt = (
                user_prompt
                + "\n\nYour previous final answer could not be parsed. "
                "Return only the required valid JSON object. Escape newlines "
                "and quotes correctly. Do not use Markdown or commentary."
            )
    raise AssertionError("unreachable")


def extract_response_text(response: dict[str, Any], mode: str) -> str:
    if mode == "chat-completions":
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ApiError(f"Unexpected chat-completions response: {response}") from exc
        if not isinstance(content, str):
            raise ApiError("Chat response content is not text")
        return content

    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    parts: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    if parts:
        return "\n".join(parts)
    output_types = [
        item.get("type")
        for item in response.get("output", [])
        if isinstance(item, dict)
    ]
    summary = {
        "id": response.get("id"),
        "status": response.get("status"),
        "model": response.get("model"),
        "output_types": output_types,
        "usage": response.get("usage"),
        "incomplete_details": response.get("incomplete_details"),
    }
    if output_types and set(output_types) <= {"reasoning"}:
        raise MissingFinalOutputError(
            "Responses API returned reasoning but no final text: "
            + json.dumps(summary, ensure_ascii=False)
        )
    raise ApiError(
        "Responses API returned no final text: "
        + json.dumps(summary, ensure_ascii=False)
    )


def extract_file_content(model_text: str) -> str:
    try:
        parsed = extract_json_object(model_text)
    except MalformedModelOutputError:
        recovered = _recover_lean_source(model_text)
        if recovered is not None:
            return recovered.rstrip() + "\n"
        raise
    content = parsed.get("content")
    if not isinstance(content, str) or not content.strip():
        raise MalformedModelOutputError(
            "Model JSON must contain a non-empty string field 'content'",
            model_text,
        )
    return content.rstrip() + "\n"


def _strip_markdown_fence(value: str) -> str | None:
    match = re.search(r"```(?:lean|lean4)?\s*\n(?P<code>.*?)```", value, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group("code").strip()
    return None


def _looks_like_lean_source(value: str) -> bool:
    clean = value.lstrip()
    starters = (
        "--", "/-", "import ", "open ", "namespace ", "section ", "noncomputable ", "set_option ",
        "theorem ", "lemma ", "def ", "abbrev ", "example ", "variable ",
    )
    return clean.startswith(starters) and any(
        token in clean for token in ("theorem ", "lemma ", "def ", "example ", "import ")
    )


def _recover_malformed_content_wrapper(value: str) -> str | None:
    clean = value.strip()
    prefix = re.match(r'^\{\s*"content"\s*:\s*"', clean)
    if prefix is None:
        return None
    end = clean.rfind('"')
    if end <= prefix.end():
        return None
    raw = clean[prefix.end() : end]
    escaped_controls = raw.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    try:
        recovered = json.loads('"' + escaped_controls + '"')
    except json.JSONDecodeError:
        recovered = raw.replace('\\"', '"').replace("\\n", "\n")
    return recovered if isinstance(recovered, str) else None


def _recover_lean_source(model_text: str) -> str | None:
    fenced = _strip_markdown_fence(model_text)
    if fenced is not None and _looks_like_lean_source(fenced):
        return fenced
    wrapped = _recover_malformed_content_wrapper(model_text)
    if wrapped is not None and _looks_like_lean_source(wrapped):
        return wrapped
    clean = model_text.strip()
    if _looks_like_lean_source(clean):
        return clean
    return None


def extract_json_object(model_text: str) -> dict[str, Any]:
    candidate = model_text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    try:
        parsed: Any = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise MalformedModelOutputError(
                "Model did not return the required JSON object", model_text
            )
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise MalformedModelOutputError(
                "Model returned malformed JSON", model_text
            ) from exc

    if not isinstance(parsed, dict):
        raise MalformedModelOutputError("Model must return a JSON object", model_text)
    return parsed
