from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lean_loop.mcp_client import HttpMcpClient, McpError, StdioMcpClient
from lean_loop.process_control import ProcessCancelled, ProcessControl


LSP_MODES = {"off", "stdio", "http"}
REQUIRED_LSP_TOOLS = {
    "lean_diagnostic_messages",
    "lean_goal",
    "lean_hover_info",
    "lean_local_search",
}
_DIAGNOSTIC_POSITION_RE = re.compile(r":(?P<line>\d+):(?P<column>\d+):")
_QUOTED_IDENTIFIER_RE = re.compile(
    r"[`'](?P<name>[A-Za-z_][A-Za-z0-9_'.]*)[`']"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.]*$")
MAX_PROMPT_EVIDENCE_CHARS = 32_000


@dataclass(frozen=True)
class LspSettings:
    mode: str = "off"
    command: str = "lean-lsp-mcp"
    url: str = "http://127.0.0.1:8000/mcp"
    startup_timeout_seconds: int = 180
    call_timeout_seconds: int = 60
    remote_search: bool = True
    max_search_terms: int = 3

    @classmethod
    def from_values(cls, values: dict[str, Any]) -> "LspSettings":
        mode = str(
            values.get("lsp_mode")
            or os.environ.get("LEAN_AGENT_LSP_MODE", "off")
        ).strip().lower()
        command = str(
            values.get("lsp_command")
            or os.environ.get("LEAN_AGENT_LSP_COMMAND", "lean-lsp-mcp")
        ).strip()
        url = str(
            values.get("lsp_url")
            or os.environ.get(
                "LEAN_AGENT_LSP_URL", "http://127.0.0.1:8000/mcp"
            )
        ).strip()
        try:
            startup_timeout = int(
                values.get("lsp_startup_timeout_seconds")
                or os.environ.get("LEAN_AGENT_LSP_STARTUP_TIMEOUT", "180")
            )
            call_timeout = int(
                values.get("lsp_call_timeout_seconds")
                or os.environ.get("LEAN_AGENT_LSP_CALL_TIMEOUT", "60")
            )
            max_search_terms = int(
                values.get("lsp_max_search_terms")
                or os.environ.get("LEAN_AGENT_LSP_MAX_SEARCH_TERMS", "3")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("LSP timeout and search limits must be integers") from exc
        remote_value = values.get("lsp_remote_search")
        remote_search = (
            bool(remote_value)
            if remote_value is not None
            else os.environ.get("LEAN_AGENT_LSP_REMOTE_SEARCH", "true")
            .strip()
            .lower()
            in {"true", "1", "yes"}
        )
        if mode not in LSP_MODES:
            raise ValueError("LSP mode must be off, stdio, or http")
        if not command:
            raise ValueError("LSP command must not be empty")
        if startup_timeout < 1 or call_timeout < 1:
            raise ValueError("LSP timeouts must be positive")
        if not 1 <= max_search_terms <= 10:
            raise ValueError("LSP max search terms must be between 1 and 10")
        if mode == "http":
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("LSP URL must be an http:// or https:// URL")
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError(
                    "HTTP LSP MCP is restricted to loopback addresses; use a local tunnel"
                )
        return cls(
            mode=mode,
            command=command,
            url=url,
            startup_timeout_seconds=startup_timeout,
            call_timeout_seconds=call_timeout,
            remote_search=remote_search,
            max_search_terms=max_search_terms,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_lsp_command(command: str) -> str:
    candidate = Path(command).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        if candidate.is_file():
            return str(candidate.resolve())
        raise FileNotFoundError(f"lean-lsp-mcp command was not found: {candidate}")
    resolved = shutil.which(command)
    if resolved:
        return resolved
    if os.name == "nt":
        local = Path.home() / ".local" / "bin" / f"{command}.exe"
        if local.is_file():
            return str(local)
    raise FileNotFoundError(
        f"lean-lsp-mcp command was not found: {command}. Install it with "
        "`uv tool install lean-lsp-mcp`."
    )


def _bounded_json(value: Any, limit: int = MAX_PROMPT_EVIDENCE_CHARS) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "\n... [LSP evidence truncated]"


def lsp_prompt_block(evidence: dict[str, Any] | None) -> str:
    if not evidence:
        return "No Lean LSP evidence was collected."
    return (
        "Lean LSP evidence (advisory; the deterministic lake check remains "
        "authoritative):\n--- lsp evidence ---\n"
        + _bounded_json(evidence)
        + "\n--- end lsp evidence ---"
    )


def _diagnostic_positions(diagnostics: str, line_count: int) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    for match in _DIAGNOSTIC_POSITION_RE.finditer(diagnostics):
        line = int(match.group("line"))
        column = int(match.group("column"))
        if 1 <= line <= line_count and (line, column) not in positions:
            positions.append((line, max(column, 1)))
        if len(positions) >= 3:
            break
    return positions


def _hover_positions(
    source: str, diagnostics: str, search_terms: list[str]
) -> list[tuple[str, int, int]]:
    names: list[str] = []
    for match in _QUOTED_IDENTIFIER_RE.finditer(diagnostics):
        name = match.group("name")
        if name not in names:
            names.append(name)
    for term in search_terms:
        if _IDENTIFIER_RE.fullmatch(term) and term not in names:
            names.append(term)
    lines = source.splitlines()
    positions: list[tuple[str, int, int]] = []
    for name in names:
        candidates = [name, name.rsplit(".", 1)[-1]]
        found = False
        for line_number, line in enumerate(lines, 1):
            for candidate in candidates:
                match = re.search(
                    rf"(?<![A-Za-z0-9_']){re.escape(candidate)}(?![A-Za-z0-9_'])",
                    line,
                )
                if match:
                    positions.append((name, line_number, match.start() + 1))
                    found = True
                    break
            if found:
                break
        if len(positions) >= 2:
            break
    return positions


class LspEvidenceCollector:
    def __init__(
        self,
        *,
        project: Path,
        settings: LspSettings,
        process_control: ProcessControl | None = None,
    ) -> None:
        self.project = project.resolve()
        self.settings = settings
        self.process_control = process_control
        self.client: StdioMcpClient | HttpMcpClient | None = None
        self.available_tools: set[str] = set()
        self.start_error: str | None = None
        self._loogle_cache: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self.settings.mode != "off"

    def start(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "settings": self.settings.to_dict()}
        if self.client is not None:
            return self.status()
        if self.start_error is not None:
            return self.status()
        try:
            if self.settings.mode == "stdio":
                command = resolve_lsp_command(self.settings.command)
                self.client = StdioMcpClient(
                    command=command,
                    args=[
                        "--transport",
                        "stdio",
                        "--lean-project-path",
                        str(self.project),
                        "--disable-tools",
                        "lean_run_code,lean_build",
                    ],
                    cwd=self.project,
                    startup_timeout_seconds=self.settings.startup_timeout_seconds,
                    call_timeout_seconds=self.settings.call_timeout_seconds,
                    control=self.process_control,
                )
            else:
                self.client = HttpMcpClient(
                    url=self.settings.url,
                    startup_timeout_seconds=self.settings.startup_timeout_seconds,
                    call_timeout_seconds=self.settings.call_timeout_seconds,
                    control=self.process_control,
                )
            self.client.start()
            self.available_tools = {
                str(row.get("name") or "") for row in self.client.list_tools()
            }
            missing = sorted(REQUIRED_LSP_TOOLS - self.available_tools)
            if missing:
                raise McpError(
                    "MCP server is missing required read-only tools: "
                    + ", ".join(missing)
                )
        except ProcessCancelled:
            self.close()
            raise
        except Exception as exc:
            self.start_error = f"{type(exc).__name__}: {exc}"
            self.close()
        return self.status()

    def status(self) -> dict[str, Any]:
        client = self.client
        return {
            "status": (
                "ready"
                if client is not None and not self.start_error
                else "unavailable"
                if self.start_error
                else "disabled"
                if not self.enabled
                else "not_started"
            ),
            "mode": self.settings.mode,
            "pid": getattr(client, "pid", None) if client is not None else None,
            "server": dict(getattr(client, "server_info", {}) or {}),
            "tools": sorted(self.available_tools),
            "error": self.start_error,
        }

    def _call(
        self,
        evidence: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        if self.client is None or tool not in self.available_tools:
            return None
        try:
            return self.client.call_tool(
                tool,
                arguments,
                timeout_seconds=timeout_seconds,
            )
        except ProcessCancelled:
            raise
        except Exception as exc:
            evidence.setdefault("tool_errors", []).append(
                {"tool": tool, "error": f"{type(exc).__name__}: {exc}"}
            )
            return None

    def collect(
        self,
        *,
        file_path: Path,
        source: str,
        diagnostics: str,
        search_terms: list[str],
        allow_remote_search: bool = True,
        total_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        status = self.start()
        evidence: dict[str, Any] = {"session": status}
        if status["status"] != "ready":
            return evidence
        deadline = (
            time.monotonic() + total_timeout_seconds
            if total_timeout_seconds is not None
            else None
        )

        def remaining_timeout() -> float | None:
            if deadline is None:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                evidence.setdefault(
                    "collection_timeout",
                    f"LSP evidence collection exceeded {total_timeout_seconds}s",
                )
                return 0
            return remaining

        def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
            remaining = remaining_timeout()
            if remaining == 0:
                return None
            return self._call(
                evidence,
                tool,
                arguments,
                timeout_seconds=remaining,
            )
        try:
            relative = file_path.resolve().relative_to(self.project).as_posix()
        except ValueError:
            evidence["tool_errors"] = [
                {"tool": "path", "error": "LSP file must stay inside the project"}
            ]
            return evidence

        lsp_diagnostics = call(
            "lean_diagnostic_messages",
            {
                "file_path": relative,
                "interactive": False,
                "timeout_s": float(
                    max(1, self.settings.call_timeout_seconds - 5)
                ),
            },
        )
        if lsp_diagnostics is not None:
            evidence["diagnostics"] = lsp_diagnostics

        lines = source.splitlines()
        goals: list[dict[str, Any]] = []
        for line, column in _diagnostic_positions(diagnostics, len(lines)):
            value = call(
                "lean_goal",
                {
                    "file_path": relative,
                    "line": line,
                    "column": column,
                    "format": "structured",
                    "timeout_s": float(
                        max(1, self.settings.call_timeout_seconds - 5)
                    ),
                },
            )
            if value is not None:
                goals.append({"line": line, "column": column, "result": value})
        if goals:
            evidence["goals"] = goals

        hovers: list[dict[str, Any]] = []
        for name, line, column in _hover_positions(
            source, diagnostics, search_terms
        ):
            value = call(
                "lean_hover_info",
                {"file_path": relative, "line": line, "column": column},
            )
            if value is not None:
                hovers.append(
                    {
                        "requested_identifier": name,
                        "line": line,
                        "column": column,
                        "result": value,
                    }
                )
        if hovers:
            evidence["hover"] = hovers

        searches: list[dict[str, Any]] = []
        remote_used = False
        unique_terms: list[str] = []
        for term in search_terms:
            cleaned = term.strip()
            if cleaned and cleaned not in unique_terms:
                unique_terms.append(cleaned)
        for term in unique_terms[: self.settings.max_search_terms]:
            local = call(
                "lean_local_search",
                {"query": term, "limit": 8, "project_root": str(self.project)},
            )
            row: dict[str, Any] = {"query": term, "local": local or {}}
            local_items = (local or {}).get("items")
            should_use_remote = (
                allow_remote_search
                and self.settings.remote_search
                and not remote_used
                and "lean_loogle" in self.available_tools
                and (not isinstance(local_items, list) or not local_items)
            )
            if should_use_remote:
                if term in self._loogle_cache:
                    row["loogle"] = self._loogle_cache[term]
                    row["loogle_cache_hit"] = True
                else:
                    remote = call(
                        "lean_loogle",
                        {"query": term, "num_results": 8},
                    )
                    if remote is not None:
                        self._loogle_cache[term] = remote
                        row["loogle"] = remote
                remote_used = True
            searches.append(row)
        if searches:
            evidence["search"] = searches
        return evidence

    def close(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            client.close()

    def __enter__(self) -> "LspEvidenceCollector":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
