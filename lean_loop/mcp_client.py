from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from lean_loop.process_control import (
    ProcessCancelled,
    ProcessControl,
    terminate_process_tree,
)


MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024


class McpError(RuntimeError):
    pass


class McpTimeoutError(McpError):
    pass


def _cancel_requested(control: ProcessControl | None) -> bool:
    return bool(control is not None and control.cancel_requested())


def _progress(control: ProcessControl | None, event: str, **details: Any) -> None:
    callback = getattr(control, "process_progress", None)
    if callable(callback):
        callback("lsp_mcp", {"event": event, **details})


def _tool_payload(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("isError"):
        messages = [
            str(row.get("text") or "")
            for row in value.get("content", [])
            if isinstance(row, dict) and row.get("type") == "text"
        ]
        raise McpError("; ".join(filter(None, messages)) or "MCP tool returned an error")
    structured = value.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    texts = [
        str(row.get("text") or "")
        for row in value.get("content", [])
        if isinstance(row, dict) and row.get("type") == "text"
    ]
    joined = "\n".join(filter(None, texts)).strip()
    if not joined:
        return {}
    try:
        parsed = json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


class StdioMcpClient:
    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        cwd: Path,
        startup_timeout_seconds: int,
        call_timeout_seconds: int,
        control: ProcessControl | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.startup_timeout_seconds = startup_timeout_seconds
        self.call_timeout_seconds = call_timeout_seconds
        self.control = control
        self.env = dict(env) if env is not None else None
        self.process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._stderr_parts: list[str] = []
        self._stderr_bytes = 0
        self._finished_reported = False
        self.server_info: dict[str, Any] = {}

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def stderr_preview(self) -> str:
        return "".join(self._stderr_parts)

    def start(self) -> None:
        if self.process is not None:
            return
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                [self.command, *self.args],
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
                env=self.env,
            )
        except OSError as exc:
            raise McpError(f"Could not start MCP server: {exc}") from exc
        if self.control is not None:
            try:
                self.control.process_started(self.process.pid, "lsp_mcp")
            except Exception:
                terminate_process_tree(self.process)
                self.process = None
                raise
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        _progress(self.control, "starting", pid=self.process.pid, transport="stdio")
        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "windows-lean-loop",
                        "version": "0.15.0",
                    },
                },
                timeout_seconds=self.startup_timeout_seconds,
            )
            self.server_info = dict(result.get("serverInfo") or {})
            self._notify("notifications/initialized", {})
            _progress(
                self.control,
                "ready",
                pid=self.process.pid,
                server=self.server_info,
            )
        except Exception:
            self.close()
            raise

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            encoded_size = len(line.encode("utf-8", errors="replace"))
            if encoded_size > MAX_MCP_MESSAGE_BYTES:
                self._messages.put(
                    McpError(f"MCP message exceeded {MAX_MCP_MESSAGE_BYTES} bytes")
                )
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                self._messages.put(McpError(f"Invalid MCP JSON: {exc}"))
                continue
            if isinstance(value, dict):
                self._messages.put(value)
        returncode = process.poll()
        if returncode is None:
            try:
                returncode = process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        self._messages.put(
            McpError(
                "MCP server closed its output stream"
                + (f" with code {returncode}" if returncode is not None else "")
                + f": {self.stderr_preview.strip()}"
            )
        )

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            if self._stderr_bytes >= MAX_STDERR_BYTES:
                continue
            remaining = MAX_STDERR_BYTES - self._stderr_bytes
            encoded = line.encode("utf-8", errors="replace")[:remaining]
            text = encoded.decode("utf-8", errors="ignore")
            self._stderr_parts.append(text)
            self._stderr_bytes += len(encoded)

    def _send(self, value: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpError("MCP server is not running")
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                process.stdin.write(line)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise McpError("MCP server closed its input stream") from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _reply_method_not_found(self, request_id: Any) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not supported"},
            }
        )

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        timeout = timeout_seconds or self.call_timeout_seconds
        deadline = time.monotonic() + timeout
        while True:
            if _cancel_requested(self.control):
                try:
                    self._notify(
                        "notifications/cancelled",
                        {"requestId": request_id, "reason": "workflow cancelled"},
                    )
                except McpError:
                    pass
                self.close()
                raise ProcessCancelled("Cancelled during LSP MCP call")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    self._notify(
                        "notifications/cancelled",
                        {"requestId": request_id, "reason": "tool timeout"},
                    )
                except McpError:
                    pass
                raise McpTimeoutError(
                    f"MCP request {method} timed out after {timeout}s"
                )
            try:
                message = self._messages.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if isinstance(message, BaseException):
                raise message
            if "method" in message and "id" in message:
                self._reply_method_not_found(message.get("id"))
                continue
            if message.get("id") != request_id:
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raise McpError(str(error.get("message") or error))
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpError(f"MCP request {method} returned no result object")
            return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools")
        return [row for row in tools if isinstance(row, dict)] if isinstance(tools, list) else []

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        _progress(self.control, "tool_started", tool=name, pid=self.pid)
        started = time.perf_counter()
        try:
            result = self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
                timeout_seconds=timeout_seconds,
            )
            return _tool_payload(result)
        finally:
            _progress(
                self.control,
                "tool_finished",
                tool=name,
                pid=self.pid,
                duration_seconds=round(time.perf_counter() - started, 6),
            )

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if self.control is not None and not self._finished_reported:
                self._finished_reported = True
                self.control.process_finished(process.pid)
            _progress(self.control, "stopped", pid=process.pid)
            self.process = None

    def __enter__(self) -> "StdioMcpClient":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class HttpMcpClient:
    def __init__(
        self,
        *,
        url: str,
        startup_timeout_seconds: int,
        call_timeout_seconds: int,
        control: ProcessControl | None = None,
    ) -> None:
        self.url = url
        self.startup_timeout_seconds = startup_timeout_seconds
        self.call_timeout_seconds = call_timeout_seconds
        self.control = control
        self._next_id = 0
        self.session_id: str | None = None
        self.server_info: dict[str, Any] = {}

    def _decode_response(self, response: Any) -> list[dict[str, Any]]:
        data = response.read(MAX_MCP_MESSAGE_BYTES + 1)
        if len(data) > MAX_MCP_MESSAGE_BYTES:
            raise McpError(f"MCP response exceeded {MAX_MCP_MESSAGE_BYTES} bytes")
        if not data:
            return []
        text = data.decode("utf-8", errors="replace")
        content_type = str(response.headers.get("Content-Type") or "").lower()
        payloads: list[str]
        if "text/event-stream" in content_type:
            payloads = [
                line.removeprefix("data:").strip()
                for line in text.splitlines()
                if line.startswith("data:") and line.removeprefix("data:").strip()
            ]
        else:
            payloads = [text]
        values: list[dict[str, Any]] = []
        for payload in payloads:
            try:
                value = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise McpError(f"Invalid HTTP MCP JSON: {exc}") from exc
            if isinstance(value, dict):
                values.append(value)
        return values

    def _post(
        self,
        value: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        if _cancel_requested(self.control):
            raise ProcessCancelled("Cancelled before HTTP LSP MCP call")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = str(session_id)
                return self._decode_response(response)
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            raise McpError(f"HTTP MCP returned {exc.code}: {body}") from exc
        except TimeoutError as exc:
            raise McpTimeoutError(
                f"HTTP MCP request timed out after {timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise McpError(f"HTTP MCP connection failed: {exc}") from exc

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        values = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
            timeout_seconds=timeout_seconds or self.call_timeout_seconds,
        )
        for value in values:
            if value.get("id") != request_id:
                continue
            error = value.get("error")
            if isinstance(error, dict):
                raise McpError(str(error.get("message") or error))
            result = value.get("result")
            if isinstance(result, dict):
                return result
        raise McpError(f"HTTP MCP request {method} returned no matching result")

    def start(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "windows-lean-loop", "version": "0.15.0"},
            },
            timeout_seconds=self.startup_timeout_seconds,
        )
        self.server_info = dict(result.get("serverInfo") or {})
        self._post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            timeout_seconds=self.call_timeout_seconds,
        )
        _progress(self.control, "ready", transport="http", server=self.server_info)

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools")
        return [row for row in tools if isinstance(row, dict)] if isinstance(tools, list) else []

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        _progress(self.control, "tool_started", tool=name, transport="http")
        started = time.perf_counter()
        try:
            return _tool_payload(
                self._request(
                    "tools/call",
                    {"name": name, "arguments": arguments},
                    timeout_seconds=timeout_seconds,
                )
            )
        finally:
            _progress(
                self.control,
                "tool_finished",
                tool=name,
                transport="http",
                duration_seconds=round(time.perf_counter() - started, 6),
            )

    def close(self) -> None:
        if not self.session_id:
            return
        request = urllib.request.Request(
            self.url,
            headers={
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": self.session_id,
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
            method="DELETE",
        )
        try:
            urllib.request.urlopen(request, timeout=2).close()
        except OSError:
            pass
        self.session_id = None

    def __enter__(self) -> "HttpMcpClient":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
