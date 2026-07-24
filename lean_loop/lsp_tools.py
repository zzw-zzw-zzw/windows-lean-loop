from __future__ import annotations

import json
import os
import re
import shutil
import sys
import textwrap
import time
from collections import Counter
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
_DECLARATION_MODIFIERS = r"(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*"
_STRUCTURAL_COMMANDS = (
    r"import|theorem|lemma|def|example|abbrev|opaque|axiom|constant|"
    r"structure|class|inductive|instance|namespace|section|mutual|end|"
    r"universe|variable|open|attribute|syntax|macro|elab"
)
_FORBIDDEN_LOCAL_SNIPPET_RE = re.compile(
    rf"(?im)(?:\b(?:sorryAx|sorry|admit)\b|^\s*{_DECLARATION_MODIFIERS}"
    rf"(?:{_STRUCTURAL_COMMANDS})\b)"
)
_STRUCTURAL_SOURCE_LINE_RE = re.compile(
    rf"^\s*{_DECLARATION_MODIFIERS}(?:{_STRUCTURAL_COMMANDS})\b"
)
_TACTIC_SOURCE_LINE_RE = re.compile(
    r"^(?:[·{}]|case\b|next\b|have\b|let\b|suffices\b|show\b|calc\b|"
    r"conv\b|exact\b|refine\b|apply\b|apply_fun\b|intro\b|intros\b|"
    r"rintro\b|use\b|constructor\b|left\b|right\b|rfl\b|trivial\b|"
    r"assumption\b|contradiction\b|exfalso\b|by_cases\b|by_contra\b|"
    r"cases\b|rcases\b|obtain\b|induction\b|subst\b|specialize\b|"
    r"generalize\b|rename_i\b|clear\b|revert\b|change\b|unfold\b|"
    r"dsimp\b|simp\b|simpa\b|simp_all\b|rw\b|nth_rw\b|erw\b|ext\b|"
    r"funext\b|set\b|wlog\b|repeat\b|try\b|first\b|solve\b|all_goals\b|"
    r"any_goals\b|aesop\b|omega\b|linarith\b|nlinarith\b|ring\b|"
    r"ring_nf\b|norm_num\b|native_decide\b|decide\b|tauto\b|grind\b|"
    r"positivity\b|field_simp\b|polyrith\b|linear_combination\b|"
    r"gcongr\b|push_neg\b|norm_cast\b|exact_mod_cast\b|lift\b|"
    r"filter_upwards\b|fin_cases\b|interval_cases\b|continuity\b|"
    r"fun_prop\b|measurability\b)"
)


@dataclass(frozen=True)
class LspSettings:
    mode: str = "off"
    command: str = "lean-lsp-mcp"
    rg_path: str = ""
    url: str = "http://127.0.0.1:8000/mcp"
    startup_timeout_seconds: int = 180
    call_timeout_seconds: int = 60
    evidence_budget_seconds: int = 60
    remote_search: bool = True
    max_search_terms: int = 3
    local_repair: bool = True
    local_max_rounds: int = 2
    local_max_candidates: int = 4
    local_validation_timeout_seconds: int = 75
    local_total_budget_seconds: int = 240
    local_reasoning_effort: str = "low"

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
        rg_path = str(
            values.get("lsp_rg_path")
            or os.environ.get("LEAN_AGENT_LSP_RG", "")
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
            evidence_budget = int(
                values.get("lsp_evidence_budget_seconds")
                or os.environ.get("LEAN_AGENT_LSP_EVIDENCE_BUDGET", "60")
            )
            max_search_terms = int(
                values.get("lsp_max_search_terms")
                or os.environ.get("LEAN_AGENT_LSP_MAX_SEARCH_TERMS", "3")
            )
            local_max_rounds = int(
                values.get("lsp_local_max_rounds")
                or os.environ.get("LEAN_AGENT_LSP_LOCAL_MAX_ROUNDS", "2")
            )
            local_max_candidates = int(
                values.get("lsp_local_max_candidates")
                or os.environ.get("LEAN_AGENT_LSP_LOCAL_MAX_CANDIDATES", "4")
            )
            local_validation_timeout = int(
                values.get("lsp_local_validation_timeout_seconds")
                or os.environ.get("LEAN_AGENT_LSP_LOCAL_VALIDATION_TIMEOUT", "75")
            )
            local_total_budget = int(
                values.get("lsp_local_total_budget_seconds")
                or os.environ.get("LEAN_AGENT_LSP_LOCAL_TOTAL_BUDGET", "240")
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
        local_repair_value = values.get("lsp_local_repair")
        local_repair = (
            bool(local_repair_value)
            if local_repair_value is not None
            else os.environ.get("LEAN_AGENT_LSP_LOCAL_REPAIR", "true")
            .strip()
            .lower()
            in {"true", "1", "yes"}
        )
        local_reasoning_effort = str(
            values.get("lsp_local_reasoning_effort")
            or os.environ.get("LEAN_AGENT_LSP_LOCAL_REASONING_EFFORT", "low")
        ).strip().lower()
        if mode not in LSP_MODES:
            raise ValueError("LSP mode must be off, stdio, or http")
        if not command:
            raise ValueError("LSP command must not be empty")
        if (
            startup_timeout < 1
            or call_timeout < 1
            or evidence_budget < 1
            or local_validation_timeout < 1
            or local_total_budget < 1
        ):
            raise ValueError("LSP timeouts must be positive")
        if not 1 <= max_search_terms <= 10:
            raise ValueError("LSP max search terms must be between 1 and 10")
        if not 1 <= local_max_rounds <= 5:
            raise ValueError("LSP local repair rounds must be between 1 and 5")
        if not 2 <= local_max_candidates <= 12:
            raise ValueError("LSP local repair candidates must be between 2 and 12")
        if local_reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("LSP local repair reasoning effort is invalid")
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
            rg_path=rg_path,
            url=url,
            startup_timeout_seconds=startup_timeout,
            call_timeout_seconds=call_timeout,
            evidence_budget_seconds=evidence_budget,
            remote_search=remote_search,
            max_search_terms=max_search_terms,
            local_repair=local_repair,
            local_max_rounds=local_max_rounds,
            local_max_candidates=local_max_candidates,
            local_validation_timeout_seconds=local_validation_timeout,
            local_total_budget_seconds=local_total_budget,
            local_reasoning_effort=local_reasoning_effort,
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


def resolve_rg_path(configured: str = "") -> str | None:
    """Find rg for the MCP child without depending on the Dashboard's PATH."""
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise FileNotFoundError(f"rg executable was not found: {candidate}")
    resolved = shutil.which("rg")
    if resolved:
        return str(Path(resolved).resolve())
    candidates = [
        Path(sys.executable).parent / "rg.exe",
        Path.home() / "scoop" / "shims" / "rg.exe",
        Path(os.environ.get("ProgramData", "C:/ProgramData"))
        / "chocolatey" / "bin" / "rg.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft" / "WinGet" / "Links" / "rg.exe",
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(
            Path(local_app_data).glob("OpenAI/Codex/bin/*/rg.exe")
        )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def stage_rg_for_child(project: Path, resolved: str) -> str:
    """Put rg in an isolated project directory to avoid Windows DLL shadowing."""
    source = Path(resolved).resolve()
    destination = project / ".lean-agent" / "tools" / "rg.exe"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (
        not destination.is_file()
        or destination.stat().st_size != source.stat().st_size
        or destination.stat().st_mtime_ns < source.stat().st_mtime_ns
    ):
        shutil.copy2(source, destination)
    return str(destination.resolve())


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


def validate_local_repair_proposal(
    value: dict[str, Any], *, max_candidates: int
) -> dict[str, Any]:
    raw = value.get("snippets")
    if not isinstance(raw, list):
        raise ValueError("Local repair JSON requires a snippets array")
    snippets: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        snippet = textwrap.dedent(item).strip()
        if (
            not snippet
            or len(snippet) > 4_000
            or "```" in snippet
            or _FORBIDDEN_LOCAL_SNIPPET_RE.search(snippet)
            or snippet in snippets
        ):
            continue
        snippets.append(snippet)
        if len(snippets) >= max_candidates:
            break
    if len(snippets) < 2:
        raise ValueError("Local repair requires at least two safe, distinct snippets")
    reason = value.get("reason")
    return {
        "snippets": snippets,
        "reason": reason.strip() if isinstance(reason, str) else "",
    }


def _local_attempt_items(value: dict[str, Any]) -> list[Any]:
    payload: Any = value
    if isinstance(payload.get("result"), dict):
        payload = payload["result"]
    items = payload.get("items") if isinstance(payload, dict) else None
    return items if isinstance(items, list) else []


def _has_open_goal(value: dict[str, Any] | None) -> bool:
    if not isinstance(value, dict):
        return False
    payload: Any = value.get("result")
    if not isinstance(payload, dict):
        payload = value
    status = str(payload.get("status") or "").lower()
    if status in {"goals", "open"}:
        return True
    for key in ("goals", "goals_before", "goals_after"):
        goals = payload.get(key)
        if isinstance(goals, list) and len(goals) > 0:
            return True
    return False


def _diagnostic_key(value: dict[str, Any], *, include_line: bool) -> tuple[Any, ...]:
    message = str(value.get("message") or "").strip()
    # Goal pretty-printing can be very large; the first line identifies the
    # diagnostic class while line/column distinguish repeated errors.
    message_head = message.splitlines()[0] if message else ""
    key: tuple[Any, ...] = (
        str(value.get("severity") or "").lower(),
        message_head,
    )
    return (*key, value.get("line"), value.get("column")) if include_line else key


def select_local_attempt(
    value: dict[str, Any],
    *,
    target_diagnostic: dict[str, Any] | None = None,
    baseline_diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Select a locally safe improvement, even when outer proof goals remain."""
    items = _local_attempt_items(value)
    baseline = [row for row in baseline_diagnostics or [] if isinstance(row, dict)]
    baseline_counts = Counter(
        _diagnostic_key(row, include_line=False) for row in baseline
    )
    target_class = (
        _diagnostic_key(target_diagnostic, include_line=False)
        if isinstance(target_diagnostic, dict)
        else None
    )
    if target_class is not None and baseline_counts[target_class] == 0:
        baseline_counts[target_class] = 1
    ranked: list[tuple[int, int, int, dict[str, Any]]] = []
    for row in items:
        if not isinstance(row, dict) or row.get("timed_out") is True:
            continue
        diagnostics = row.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, list) else []
        errors = [
            item
            for item in diagnostics
            if isinstance(item, dict)
            and str(item.get("severity") or "").lower() == "error"
        ]
        hard_errors = [
            item
            for item in errors
            if "unsolvedGoals" not in (item.get("lean_tags") or [])
        ]
        candidate_counts = Counter(
            _diagnostic_key(item, include_line=False) for item in hard_errors
        )
        if (
            target_class is not None
            and candidate_counts[target_class] >= baseline_counts[target_class]
        ):
            continue
        if any(count > baseline_counts[key] for key, count in candidate_counts.items()):
            continue
        proof_status = str(row.get("proof_status") or "").lower()
        goals = row.get("goals")
        complete = "completed" in proof_status or goals == []
        goal_count = len(goals) if isinstance(goals, list) else 1_000_000
        ranked.append((0 if complete else 1, len(hard_errors), goal_count, row))
    return min(ranked, key=lambda item: item[:3], default=(0, 0, 0, None))[3]


def classify_local_target(source_line: str) -> str:
    stripped = source_line.strip()
    if not stripped or _STRUCTURAL_SOURCE_LINE_RE.match(source_line):
        return "unsupported"
    if re.search(r":=\s*by(?:\s|$)", stripped):
        return "proof_tail"
    if ":=" in stripped:
        return "unsupported"
    return "tactic_line" if _TACTIC_SOURCE_LINE_RE.match(stripped) else "unsupported"


def prepare_local_snippets(
    *, source_line: str, target_kind: str, snippets: list[str]
) -> list[str]:
    """Convert model tactics to exact whole-line replacements for MCP testing."""
    if target_kind == "tactic_line":
        return list(snippets)
    if target_kind != "proof_tail":
        return []
    match = re.match(r"^(?P<prefix>.*?:=)\s*by(?:\s+.*)?$", source_line.strip())
    if match is None:
        return []
    prefix = match.group("prefix")
    prepared: list[str] = []
    for snippet in snippets:
        normalized = textwrap.dedent(snippet).strip()
        proof = normalized if normalized.startswith("by") else f"by\n  {normalized}"
        replacement = f"{prefix} {proof}"
        if replacement not in prepared:
            prepared.append(replacement)
    return prepared


def apply_line_local_snippet(
    source: str,
    *,
    line: int,
    expected_line: str,
    snippet: str,
) -> str:
    lines = source.splitlines()
    if not 1 <= line <= len(lines):
        raise ValueError("Local repair line is outside the candidate")
    current = lines[line - 1]
    if current != expected_line:
        raise ValueError("Local repair base line changed after LSP analysis")
    if _STRUCTURAL_SOURCE_LINE_RE.match(current):
        raise ValueError("Local repair cannot replace a structural Lean line")
    indent = current[: len(current) - len(current.lstrip())]
    normalized = textwrap.dedent(snippet).strip()
    if not normalized or _FORBIDDEN_LOCAL_SNIPPET_RE.search(normalized):
        raise ValueError("Local repair snippet is empty or structurally unsafe")
    replacement = [indent + row if row else "" for row in normalized.splitlines()]
    updated = [*lines[: line - 1], *replacement, *lines[line:]]
    return "\n".join(updated) + ("\n" if source.endswith("\n") else "")


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
        self.rg_path: str | None = None
        self.rg_error: str | None = None

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
                try:
                    self.rg_path = resolve_rg_path(self.settings.rg_path)
                except FileNotFoundError as exc:
                    self.rg_error = str(exc)
                child_env = os.environ.copy()
                if self.rg_path:
                    source_dir = Path(self.rg_path).parent.resolve()
                    path_entries: list[str] = []
                    for raw_entry in child_env.get("PATH", "").split(os.pathsep):
                        entry = raw_entry.strip().strip('"')
                        if not entry:
                            continue
                        try:
                            directory = Path(entry).resolve()
                            contains_rg = any(
                                (directory / name).is_file()
                                for name in ("rg.exe", "rg.EXE", "rg")
                            )
                        except OSError:
                            directory = Path(entry)
                            contains_rg = False
                        if directory != source_dir and not contains_rg:
                            path_entries.append(entry)
                    try:
                        self.rg_path = stage_rg_for_child(self.project, self.rg_path)
                    except OSError as exc:
                        self.rg_error = f"Could not stage rg.exe: {exc}"
                        self.rg_path = None
                    if self.rg_path:
                        # The isolated directory contains no unrelated DLLs.
                        path_entries.insert(0, str(Path(self.rg_path).parent))
                    child_env["PATH"] = os.pathsep.join(path_entries)
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
                    env=child_env,
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
            "rg": {
                "status": "ready" if self.rg_path else "unavailable",
                "path": self.rg_path,
                "error": self.rg_error,
            },
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
            if (
                "." in term
                and (not isinstance(local_items, list) or not local_items)
            ):
                fallback_query = term.rsplit(".", 1)[-1]
                fallback = call(
                    "lean_local_search",
                    {
                        "query": fallback_query,
                        "limit": 8,
                        "project_root": str(self.project),
                    },
                )
                fallback_items = (fallback or {}).get("items")
                if isinstance(fallback_items, list) and fallback_items:
                    local = fallback
                    local_items = fallback_items
                    row["local"] = fallback
                    row["local_fallback_query"] = fallback_query
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

    def local_repair_context(
        self,
        *,
        file_path: Path,
        source: str,
        total_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Return one bounded, line-local repair target without modifying the file."""
        status = self.start()
        result: dict[str, Any] = {"session": status, "status": "unavailable"}
        if status["status"] != "ready":
            return result
        if "lean_multi_attempt" not in self.available_tools:
            result["reason"] = "lean_multi_attempt_not_available"
            return result
        try:
            relative = file_path.resolve().relative_to(self.project).as_posix()
        except ValueError:
            result["reason"] = "file_outside_project"
            return result
        deadline = (
            time.monotonic() + total_timeout_seconds
            if total_timeout_seconds is not None
            else None
        )

        def timeout() -> float:
            if deadline is None:
                return float(self.settings.call_timeout_seconds)
            return max(
                0.1,
                min(
                    float(self.settings.call_timeout_seconds),
                    deadline - time.monotonic(),
                ),
            )

        diagnostics = self._call(
            result,
            "lean_diagnostic_messages",
            {
                "file_path": relative,
                "interactive": False,
                "timeout_s": float(max(1, timeout() - 2)),
            },
            timeout_seconds=timeout(),
        )
        result["diagnostics"] = diagnostics or {}
        if diagnostics is None:
            result["reason"] = "diagnostics_unavailable"
            return result
        items: Any = diagnostics
        if isinstance(items, dict) and isinstance(items.get("result"), dict):
            items = items["result"]
        diagnostics_payload = items if isinstance(items, dict) else {}
        if isinstance(items, dict):
            items = items.get("items")
        errors = [
            row
            for row in items or []
            if isinstance(row, dict)
            and str(row.get("severity") or "").lower() == "error"
            and isinstance(row.get("line"), int)
        ]
        if not errors:
            if diagnostics_payload.get("timed_out") is True:
                result["reason"] = "diagnostics_timed_out"
                return result
            if diagnostics_payload.get("partial") is True:
                result["reason"] = "diagnostics_incomplete"
                return result
            result["status"] = "no_error"
            return result
        first = errors[0]
        line = int(first["line"])
        column = max(1, int(first.get("column") or 1))
        lines = source.splitlines()
        if not 1 <= line <= len(lines):
            result["reason"] = "diagnostic_position_out_of_range"
            return result
        if _STRUCTURAL_SOURCE_LINE_RE.match(lines[line - 1]):
            result["status"] = "unsupported"
            result["reason"] = "diagnostic_is_on_a_structural_line"
            return result
        target_kind = classify_local_target(lines[line - 1])
        if target_kind == "unsupported":
            result["status"] = "unsupported"
            result["reason"] = "diagnostic_is_not_a_safe_line_local_target"
            result["line"] = line
            result["source_line"] = lines[line - 1]
            return result
        goal = self._call(
            result,
            "lean_goal",
            {
                "file_path": relative,
                "line": line,
                "column": column,
                "format": "structured",
                "timeout_s": float(max(1, timeout() - 2)),
            },
            timeout_seconds=timeout(),
        )
        result["goal"] = goal or {}
        if not _has_open_goal(goal):
            result["status"] = "unsupported"
            result["reason"] = "diagnostic_has_no_open_proof_goal"
            return result
        actions = None
        if deadline is None or deadline - time.monotonic() >= 5:
            actions = self._call(
                result,
                "lean_code_actions",
                {"file_path": str(file_path.resolve()), "line": line},
                timeout_seconds=timeout(),
            )
        result.update(
            {
                "status": "target",
                "line": line,
                "column": column,
                "source_line": lines[line - 1],
                "target_kind": target_kind,
                "diagnostic": first,
                "baseline_diagnostics": errors,
                "code_actions": actions or {},
            }
        )
        return result

    def try_local_snippets(
        self,
        *,
        file_path: Path,
        line: int,
        column: int | None = None,
        snippets: list[str],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        status = self.start()
        if status["status"] != "ready" or self.client is None:
            return {"items": [], "session": status}
        relative = file_path.resolve().relative_to(self.project).as_posix()
        arguments: dict[str, Any] = {
            "file_path": relative,
            "line": line,
            "snippets": snippets[: self.settings.local_max_candidates],
        }
        if column is not None:
            arguments["column"] = column
        value = self.client.call_tool(
            "lean_multi_attempt",
            arguments,
            timeout_seconds=timeout_seconds,
        )
        return value

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
