from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from lean_loop.agent_protocol import AgentRequest, protocol_capabilities
from lean_loop.api import ApiError, call_model, effective_api_transport
from lean_loop.config import ApiConfig, ConfigError
from lean_loop.dashboard import _resolve_or_create_target, serve_dashboard
from lean_loop.explanation import generate_workflow_explanation
from lean_loop.lean import (
    ProjectError,
    check_lean,
    find_program,
    resolve_project,
    resolve_target,
)
from lean_loop.mathlib_search import collect_retrieval, search_mathlib
from lean_loop.mathlib_search import suggest_imports
from lean_loop.mathlib_index import build_mathlib_index, index_status
from lean_loop.lsp_tools import LspEvidenceCollector, LspSettings, resolve_lsp_command
from lean_loop.project_config import load_project_config
from lean_loop.queue import QueueError, QueueStore, work_queue
from lean_loop.retrieval_cache import RetrievalCache, clear_retrieval_cache
from lean_loop.runner import run_repair_loop
from lean_loop.state import WorkflowStore, list_workflows
from lean_loop.subscription_backend import (
    SUBSCRIPTION_BACKENDS,
    SubscriptionBackendError,
    create_subscription_backend,
    inspect_subscription_backend,
)
from lean_loop.workflow import run_structured_workflow


AGENT_BACKEND_CHOICES = ("direct", "codex-subscription", "claude-subscription")


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lean-loop",
        description="Repair Lean files using a selectable API transport and local verification.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "agent-protocol", help="Show the versioned Agent protocol capabilities"
    )

    doctor = subparsers.add_parser("doctor", help="Check Windows tooling and project layout")
    doctor.add_argument("--project", type=Path, required=True)
    doctor.add_argument("--agent-backend", choices=AGENT_BACKEND_CHOICES, default="direct")
    doctor.add_argument("--model", default="")
    doctor.add_argument("--reasoning-effort", default="low")

    dashboard = subparsers.add_parser(
        "dashboard", help="Start the local workflow and queue dashboard"
    )
    dashboard.add_argument("--project", type=Path, required=True)
    dashboard.add_argument("--port", type=int, default=8765)

    api_check = subparsers.add_parser(
        "api-check", help="Send a minimal API request without modifying Lean files"
    )
    api_check.add_argument("--timeout", type=int, default=60)
    api_check.add_argument("--project", type=Path, default=None)
    api_check.add_argument("--provider", default="default")
    api_check.add_argument("--agent-backend", choices=AGENT_BACKEND_CHOICES, default="direct")
    api_check.add_argument("--api-retries", type=int, default=None)
    api_check.add_argument(
        "--transport", choices=("auto", "python", "curl"), default=None
    )
    api_check.add_argument(
        "--model", default="", help="Override LEAN_AGENT_MODEL for this check"
    )
    api_check.add_argument("--reasoning-effort", default="low")
    api_check.add_argument("--temp-dir", type=Path, default=Path(".lean-agent-tmp"))

    check = subparsers.add_parser("check", help="Run Lean on one file")
    check.add_argument("--project", type=Path, required=True)
    check.add_argument("--file", required=True)
    check.add_argument("--timeout", type=int, default=120)
    check.add_argument(
        "--lake",
        default=os.environ.get("LEAN_AGENT_LAKE", "lake"),
        help="lake executable or full path (default: LEAN_AGENT_LAKE or lake)",
    )

    lsp_check = subparsers.add_parser(
        "lsp-check", help="Start or connect to Lean LSP MCP and collect evidence"
    )
    lsp_check.add_argument("--project", type=Path, required=True)
    lsp_check.add_argument("--file", required=True)
    lsp_check.add_argument("--query", action="append", default=[])
    lsp_check.add_argument("--mode", choices=("stdio", "http"), default=None)
    lsp_check.add_argument("--mcp-command", dest="lsp_command", default="")
    lsp_check.add_argument("--url", default="")
    lsp_check.add_argument("--startup-timeout", type=int, default=None)
    lsp_check.add_argument("--call-timeout", type=int, default=None)
    lsp_check.add_argument("--total-timeout", type=int, default=180)
    lsp_check.add_argument(
        "--remote-search", action=argparse.BooleanOptionalAction, default=None
    )

    search = subparsers.add_parser(
        "mathlib-search", help="Search the exact local Mathlib source tree"
    )
    search.add_argument("--project", type=Path, required=True)
    search.add_argument("--query", action="append", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--json", action="store_true")
    search.add_argument(
        "--suggest-imports", action="store_true", help="Print precise import candidates"
    )

    mathlib_index = subparsers.add_parser(
        "mathlib-index", help="Build and inspect the persistent Mathlib index"
    )
    index_commands = mathlib_index.add_subparsers(
        dest="index_command", required=True
    )
    index_build = index_commands.add_parser("build", help="Build the local index")
    index_build.add_argument("--project", type=Path, required=True)
    index_build.add_argument("--force", action="store_true")
    index_status_parser = index_commands.add_parser(
        "status", help="Show index and retrieval-cache status"
    )
    index_status_parser.add_argument("--project", type=Path, required=True)
    index_cache_clear = index_commands.add_parser(
        "clear-cache", help="Delete persistent retrieval cache entries"
    )
    index_cache_clear.add_argument("--project", type=Path, required=True)
    index_benchmark = index_commands.add_parser(
        "benchmark", help="Measure indexed retrieval and persistent cache"
    )
    index_benchmark.add_argument("--project", type=Path, required=True)
    index_benchmark.add_argument("--query", required=True)

    workflow = subparsers.add_parser(
        "workflow", help="Structured plan -> prove -> review workflows"
    )
    workflow_commands = workflow.add_subparsers(dest="workflow_command", required=True)

    workflow_run = workflow_commands.add_parser("run", help="Start a structured workflow")
    workflow_run.add_argument("--project", type=Path, required=True)
    workflow_run.add_argument("--file", required=True)
    workflow_run.add_argument("--task", required=True)
    workflow_run.add_argument("--agent-backend", choices=AGENT_BACKEND_CHOICES, default="direct")
    workflow_run.add_argument(
        "--model", default="", help="Override LEAN_AGENT_MODEL for this workflow"
    )
    workflow_run.add_argument("--max-attempts", type=int, default=3)
    workflow_run.add_argument("--max-attempts-per-step", type=int, default=3)
    workflow_run.add_argument(
        "--planning-mode",
        choices=("planner", "direct", "direct-then-planner"),
        default="planner",
    )
    workflow_run.add_argument("--lean-timeout", type=int, default=120)
    workflow_run.add_argument(
        "--formalize-goal", action=argparse.BooleanOptionalAction, default=True
    )
    workflow_run.add_argument(
        "--import-policy",
        choices=("auto", "proof-first", "precise", "broad"),
        default="auto",
    )
    workflow_run.add_argument("--api-timeout", type=int, default=None)
    workflow_run.add_argument("--api-retries", type=int, default=None)
    workflow_run.add_argument("--plan-effort", default="high")
    workflow_run.add_argument("--prove-effort", default="high")
    workflow_run.add_argument("--review-effort", default="medium")
    workflow_run.add_argument(
        "--review-model",
        default=os.environ.get("LEAN_AGENT_REVIEW_MODEL", ""),
        help="Optional review model; defaults to LEAN_AGENT_REVIEW_MODEL or the main model",
    )
    workflow_run.add_argument(
        "--lake", default=os.environ.get("LEAN_AGENT_LAKE", "lake")
    )
    workflow_run.add_argument("--keep-failed", action="store_true")
    workflow_run.add_argument(
        "--protect-declaration",
        action="append",
        default=[],
        help="Freeze one existing declaration body exactly; may be repeated",
    )
    workflow_run.add_argument(
        "--explain",
        action="store_true",
        help="Generate a natural-language proof after Lean succeeds",
    )
    workflow_run.add_argument("--explain-language", default="zh-CN")
    workflow_run.add_argument("--explain-effort", default="medium")
    workflow_run.add_argument(
        "--explain-model",
        default=os.environ.get("LEAN_AGENT_EXPLAIN_MODEL", ""),
        help="Optional explanation model; defaults to LEAN_AGENT_EXPLAIN_MODEL or the main model",
    )

    workflow_list = workflow_commands.add_parser("list", help="List workflow runs")
    workflow_list.add_argument("--project", type=Path, required=True)
    workflow_list.add_argument("--json", action="store_true")

    workflow_show = workflow_commands.add_parser("show", help="Show one workflow manifest")
    workflow_show.add_argument("--project", type=Path, required=True)
    workflow_show.add_argument("--run-id", required=True)

    workflow_timings = workflow_commands.add_parser(
        "timings", help="Show per-phase workflow timings"
    )
    workflow_timings.add_argument("--project", type=Path, required=True)
    workflow_timings.add_argument("--run-id", required=True)
    workflow_timings.add_argument("--json", action="store_true")

    workflow_explain = workflow_commands.add_parser(
        "explain", help="Explain the checked proof from a succeeded workflow"
    )
    workflow_explain.add_argument("--project", type=Path, required=True)
    workflow_explain.add_argument("--run-id", required=True)
    workflow_explain.add_argument("--language", default="zh-CN")
    workflow_explain.add_argument("--effort", default="medium")
    workflow_explain.add_argument("--api-timeout", type=int, default=None)
    workflow_explain.add_argument(
        "--model", default=os.environ.get("LEAN_AGENT_EXPLAIN_MODEL", "")
    )

    workflow_resume = workflow_commands.add_parser(
        "resume", help="Resume the saved Plan and checkpoints of a failed workflow"
    )
    workflow_resume.add_argument("--project", type=Path, required=True)
    workflow_resume.add_argument("--run-id", required=True)
    workflow_resume.add_argument("--agent-backend", choices=AGENT_BACKEND_CHOICES, default=None)
    workflow_resume.add_argument("--max-attempts", type=int, default=None)
    workflow_resume.add_argument("--max-attempts-per-step", type=int, default=None)
    workflow_resume.add_argument(
        "--planning-mode",
        choices=("planner", "direct", "direct-then-planner"),
        default=None,
    )
    workflow_resume.add_argument("--api-timeout", type=int, default=None)
    workflow_resume.add_argument("--api-retries", type=int, default=None)
    workflow_resume.add_argument("--model", default="")
    workflow_resume.add_argument("--plan-effort", default=None)
    workflow_resume.add_argument("--prove-effort", default=None)
    workflow_resume.add_argument("--review-effort", default=None)
    workflow_resume.add_argument("--review-model", default="")
    workflow_resume.add_argument("--lean-timeout", type=int, default=None)
    workflow_resume.add_argument("--lake", default="")
    workflow_resume.add_argument(
        "--import-policy",
        choices=("auto", "proof-first", "precise", "broad"),
        default=None,
    )

    queue = subparsers.add_parser(
        "queue", help="Persistent multi-file Lean task queue"
    )
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)

    queue_add = queue_commands.add_parser("add", help="Add one Lean workflow task")
    queue_add.add_argument("--project", type=Path, required=True)
    queue_add.add_argument(
        "--file", default="", help="Target .lean file; omit to create GeneratedProof_*.lean"
    )
    queue_add.add_argument("--task", required=True)
    queue_add.add_argument("--agent-backend", choices=AGENT_BACKEND_CHOICES, default="direct")
    queue_add.add_argument(
        "--model", default="", help="Override LEAN_AGENT_MODEL for this task"
    )
    queue_add.add_argument("--provider", default="default")
    queue_add.add_argument("--depends-on", action="append", default=[])
    queue_add.add_argument("--max-attempts", type=int, default=3)
    queue_add.add_argument("--max-attempts-per-step", type=int, default=3)
    queue_add.add_argument(
        "--planning-mode",
        choices=("planner", "direct", "direct-then-planner"),
        default="planner",
    )
    queue_add.add_argument("--lean-timeout", type=int, default=120)
    queue_add.add_argument(
        "--formalize-goal", action=argparse.BooleanOptionalAction, default=True
    )
    queue_add.add_argument(
        "--import-policy",
        choices=("auto", "proof-first", "precise", "broad"),
        default="auto",
    )
    queue_add.add_argument("--api-timeout", type=int, default=None)
    queue_add.add_argument("--api-retries", type=int, default=None)
    queue_add.add_argument("--plan-effort", default="high")
    queue_add.add_argument("--prove-effort", default="high")
    queue_add.add_argument("--review-effort", default="medium")
    queue_add.add_argument(
        "--review-model", default=os.environ.get("LEAN_AGENT_REVIEW_MODEL", "")
    )
    queue_add.add_argument(
        "--lake", default=os.environ.get("LEAN_AGENT_LAKE", "lake")
    )
    queue_add.add_argument("--keep-failed", action="store_true")
    queue_add.add_argument("--protect-declaration", action="append", default=[])
    queue_add.add_argument("--explain", action="store_true")
    queue_add.add_argument("--explain-language", default="zh-CN")
    queue_add.add_argument("--explain-effort", default="medium")
    queue_add.add_argument(
        "--explain-model", default=os.environ.get("LEAN_AGENT_EXPLAIN_MODEL", "")
    )

    queue_list = queue_commands.add_parser("list", help="List queued tasks")
    queue_list.add_argument("--project", type=Path, required=True)
    queue_list.add_argument("--json", action="store_true")

    queue_show = queue_commands.add_parser("show", help="Show a task and its events")
    queue_show.add_argument("--project", type=Path, required=True)
    queue_show.add_argument("--task-id", required=True)

    queue_work = queue_commands.add_parser(
        "work", help="Process ready tasks in dependency order"
    )
    queue_work.add_argument("--project", type=Path, required=True)
    queue_work.add_argument(
        "--once", action="store_true", help="Process at most one ready task"
    )

    queue_cancel = queue_commands.add_parser(
        "cancel", help="Cancel a queued or running task"
    )
    queue_cancel.add_argument("--project", type=Path, required=True)
    queue_cancel.add_argument("--task-id", required=True)

    queue_retry = queue_commands.add_parser(
        "retry", help="Put a failed or cancelled task back in the queue"
    )
    queue_retry.add_argument("--project", type=Path, required=True)
    queue_retry.add_argument("--task-id", required=True)

    run = subparsers.add_parser("run", help="Ask the model to repair one Lean file")
    run.add_argument("--project", type=Path, required=True)
    run.add_argument("--file", required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--lean-timeout", type=int, default=120)
    run.add_argument(
        "--api-timeout",
        type=int,
        default=None,
        help="API request timeout in seconds (overrides LEAN_AGENT_TIMEOUT_SECONDS)",
    )
    run.add_argument(
        "--reasoning-effort",
        default=None,
        help="Model reasoning effort for this run, e.g. low, high, xhigh",
    )
    run.add_argument(
        "--lake",
        default=os.environ.get("LEAN_AGENT_LAKE", "lake"),
        help="lake executable or full path (default: LEAN_AGENT_LAKE or lake)",
    )
    run.add_argument(
        "--keep-failed",
        action="store_true",
        help="Keep the last failed model edit instead of restoring the original file",
    )
    return parser


def _doctor(
    project_arg: Path,
    *,
    agent_backend: str = "direct",
    model: str = "",
    reasoning_effort: str = "low",
) -> int:
    project = resolve_project(project_arg)
    report = {
        "project": str(project),
        "lean_toolchain": (project / "lean-toolchain").read_text(encoding="utf-8").strip(),
        "programs": {
            name: find_program(name) for name in ("elan", "lake", "lean", "curl.exe")
        },
        "optional_programs": {
            "uv": find_program("uv"),
            "uvx": find_program("uvx"),
            "lean-lsp-mcp": None,
        },
        "api_environment": {
            "LEAN_AGENT_API_BASE": bool(_environment_value("LEAN_AGENT_API_BASE")),
            "API_KEY": bool(
                _environment_value("LEAN_AGENT_API_KEY")
                or _environment_value("OPENAI_API_KEY")
            ),
            "LEAN_AGENT_MODEL": bool(_environment_value("LEAN_AGENT_MODEL")),
        },
    }
    try:
        report["optional_programs"]["lean-lsp-mcp"] = resolve_lsp_command(
            LspSettings.from_values(load_project_config(project)).command
        )
    except (FileNotFoundError, ValueError):
        pass
    if agent_backend in SUBSCRIPTION_BACKENDS:
        try:
            config = ApiConfig.for_backend(
                project,
                agent_backend,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            backend = create_subscription_backend(
                agent_backend,
                protected_root=project,
                protected_target=project / "__doctor_no_target__",
            )
            report["agent_backend"] = inspect_subscription_backend(
                backend,
                model=config.model,
                reasoning_effort=str(config.reasoning_effort or ""),
            )
        except (ConfigError, SubscriptionBackendError) as exc:
            report["agent_backend"] = {
                "status": "blocked",
                "backend_id": agent_backend,
                "error_kind": getattr(exc, "kind", "configuration_error"),
                "message": str(exc),
            }
    else:
        report["agent_backend"] = {"status": "ready", "backend_id": "direct"}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    programs_ready = all(report["programs"].values())
    backend_ready = report["agent_backend"].get("status") == "ready"
    return 0 if programs_ready and backend_ready else 1


def _environment_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def _config_with_timeout(config: ApiConfig, timeout: int | None) -> ApiConfig:
    if timeout is None:
        return config
    if timeout < 1:
        raise ConfigError("--api-timeout must be positive")
    return replace(config, timeout_seconds=timeout)


def _print_workflow_list(project: Path, as_json: bool) -> int:
    rows = list_workflows(project)
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No structured workflows found.")
        return 0
    for row in rows:
        print(
            f"{row.get('run_id')}  {row.get('status'):9}  "
            f"{row.get('phase'):8}  {row.get('target_file')}"
        )
    return 0


def _print_queue_list(store: QueueStore, as_json: bool) -> int:
    rows = store.list_tasks()
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No queue tasks found.")
        return 0
    for row in rows:
        process = (
            f" {row['active_kind']}:{row['active_pid']}"
            if row.get("active_pid")
            else ""
        )
        print(
            f"{row['id']}  {row['state']:13}  {row['target_file']}"
            f"  attempt={row.get('attempt') or '-'}{process}"
        )
    return 0


def _queue_settings(args: argparse.Namespace) -> dict[str, object]:
    return {
        "agent_backend": args.agent_backend,
        "provider": args.provider,
        "model": args.model,
        "max_attempts": args.max_attempts,
        "max_attempts_per_step": args.max_attempts_per_step,
        "planning_mode": args.planning_mode,
        "lean_timeout": args.lean_timeout,
        "api_timeout": args.api_timeout,
        "api_retries": args.api_retries,
        "plan_effort": args.plan_effort,
        "prove_effort": args.prove_effort,
        "review_effort": args.review_effort,
        "review_model": args.review_model,
        "lake": args.lake,
        "keep_failed": args.keep_failed,
        "formalize_goal": args.formalize_goal,
        "import_policy": args.import_policy,
        "protect_existing_statements": True,
        "protected_declarations": list(args.protect_declaration),
        "explain": args.explain,
        "explain_language": args.explain_language,
        "explain_effort": args.explain_effort,
        "explain_model": args.explain_model,
    }


def _print_timings(value: dict[str, object], as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0
    print(f"Status: {value.get('status')}")
    print(f"Total: {float(value.get('total_seconds', 0)):.3f}s")
    phase_seconds = value.get("phase_seconds", {})
    phase_counts = value.get("phase_counts", {})
    if isinstance(phase_seconds, dict):
        for phase, seconds in phase_seconds.items():
            count = phase_counts.get(phase, 0) if isinstance(phase_counts, dict) else 0
            print(f"{phase:24} {float(seconds):9.3f}s  count={count}")
    return 0


def main(argv: list[str] | None = None) -> None:
    _configure_console_encoding()
    args = _parser().parse_args(argv)
    try:
        if args.command == "agent-protocol":
            print(json.dumps(protocol_capabilities(), indent=2, ensure_ascii=False))
            raise SystemExit(0)

        if args.command == "doctor":
            raise SystemExit(
                _doctor(
                    args.project,
                    agent_backend=args.agent_backend,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                )
            )

        if args.command == "dashboard":
            project = resolve_project(args.project)
            serve_dashboard(project, args.port)
            raise SystemExit(0)

        if args.command == "api-check":
            api_project = resolve_project(args.project) if args.project else None
            base_config = ApiConfig.for_backend(
                api_project,
                args.agent_backend,
                provider_id=args.provider,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
            if args.api_retries is not None:
                if args.api_retries < 0:
                    raise ConfigError("--api-retries cannot be negative")
                base_config = replace(
                    base_config, api_timeout_retries=args.api_retries
                )
            config = replace(
                base_config,
                timeout_seconds=args.timeout,
                reasoning_effort=args.reasoning_effort,
                api_transport=args.transport or base_config.api_transport,
            )
            if args.agent_backend == "direct":
                content = call_model(
                    config,
                    'Connectivity test. Return exactly {"content":"API_OK"}.',
                    args.temp_dir.resolve(),
                )
                print(f"API endpoint: {config.endpoint}")
                print(f"API transport: {effective_api_transport(config)}")
                print(f"API result: {content.strip()}")
            else:
                protected_root = api_project or Path.cwd().resolve()
                backend = create_subscription_backend(
                    args.agent_backend,
                    protected_root=protected_root,
                    protected_target=protected_root / "__api_check_no_target__",
                )
                request = AgentRequest(
                    request_id=uuid.uuid4().hex,
                    sequence=1,
                    role="planner",
                    run_id="api-check",
                    phase="connectivity",
                    output_type="json",
                    model=config.model,
                    reasoning_effort=config.reasoning_effort,
                    system_prompt=(
                        "Connectivity test in an ephemeral repo-external sandbox. "
                        "Tools may operate only inside that sandbox; protected state "
                        "must remain unchanged."
                    ),
                    user_prompt='Return exactly {"content":"API_OK"}.',
                )
                content = backend.invoke(request, config, args.temp_dir.resolve())
                print(f"Agent backend: {args.agent_backend}")
                print(f"Agent result: {json.dumps(content, ensure_ascii=False)}")
                print(
                    "Agent metadata: "
                    + json.dumps(backend.last_metadata, ensure_ascii=False, sort_keys=True)
                )
            raise SystemExit(0)

        if args.command == "mathlib-index":
            project = resolve_project(args.project)
            if args.index_command == "build":
                last_report = 0

                def report(current: int, total: int, path: Path) -> None:
                    nonlocal last_report
                    if current == total or current - last_report >= 500:
                        print(f"Indexing Mathlib: {current}/{total}  {path.name}")
                        last_report = current

                result = build_mathlib_index(
                    project,
                    force=args.force,
                    progress=report,
                )
                print("MATHLIB INDEX READY")
                print(f"Path: {result.path}")
                print(f"Files: {result.files}")
                print(f"Symbols: {result.symbols}")
                print(f"Size: {result.size_bytes / (1024 * 1024):.2f} MB")
                print(f"Build time: {result.duration_seconds:.3f}s")
                raise SystemExit(0)
            if args.index_command == "status":
                print(
                    json.dumps(
                        {
                            "index": index_status(project),
                            "retrieval_cache": RetrievalCache(project).status(),
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                raise SystemExit(0)
            if args.index_command == "clear-cache":
                clear_retrieval_cache(project)
                print("Retrieval cache cleared.")
                raise SystemExit(0)
            if args.index_command == "benchmark":
                for label in ("first", "second"):
                    started = time.perf_counter()
                    result = collect_retrieval(
                        project,
                        diagnostics="",
                        requested_terms=[args.query],
                    )
                    duration = time.perf_counter() - started
                    cache = result.get("cache", {})
                    print(
                        f"{label}: {duration:.6f}s  "
                        f"backend={result.get('search_backend')} "
                        f"cache_hit={cache.get('hit') if isinstance(cache, dict) else None} "
                        f"hits={len(result.get('hits', []))}"
                    )
                raise SystemExit(0)

        if args.command == "queue":
            project = resolve_project(args.project)
            store = QueueStore(project)
            if args.queue_command == "add":
                target, created = _resolve_or_create_target(project, args.file)
                if args.max_attempts < 1:
                    raise ConfigError("--max-attempts must be positive")
                if args.max_attempts_per_step < 1:
                    raise ConfigError("--max-attempts-per-step must be positive")
                if args.lean_timeout < 1:
                    raise ConfigError("--lean-timeout must be positive")
                if args.api_timeout is not None and args.api_timeout < 1:
                    raise ConfigError("--api-timeout must be positive")
                if args.api_retries is not None and args.api_retries < 0:
                    raise ConfigError("--api-retries cannot be negative")
                row = store.add_task(
                    target_file=target.relative_to(project).as_posix(),
                    task_text=args.task,
                    settings=_queue_settings(args),
                    dependencies=args.depends_on,
                )
                print(f"QUEUED: {row['id']}")
                print(f"File: {row['target_file']}")
                if created:
                    print("A new Lean target file was created.")
                raise SystemExit(0)
            if args.queue_command == "list":
                raise SystemExit(_print_queue_list(store, args.json))
            if args.queue_command == "show":
                row = store.get_task(args.task_id, include_events=True)
                if row.get("workflow_run_id"):
                    timing_path = (
                        project
                        / ".lean-agent"
                        / "workflows"
                        / str(row["workflow_run_id"])
                        / "timings.json"
                    )
                    if timing_path.is_file():
                        row["workflow_timings"] = json.loads(
                            timing_path.read_text(encoding="utf-8")
                        )
                print(json.dumps(row, indent=2, ensure_ascii=False))
                raise SystemExit(0)
            if args.queue_command == "cancel":
                row = store.request_cancel(args.task_id)
                print(f"Task {row['id']}: {row['state']}")
                if row["state"] in {"planning", "proving", "lean_checking", "reviewing", "auditing", "explaining"}:
                    print("Cancellation requested; the worker will terminate its active process tree.")
                raise SystemExit(0)
            if args.queue_command == "retry":
                row = store.retry(args.task_id)
                print(f"Task {row['id']}: {row['state']}")
                raise SystemExit(0)
            if args.queue_command == "work":
                result = work_queue(
                    project=project,
                    once=args.once,
                    progress=lambda row: print(
                        f"WORKING: {row['id']}  {row['target_file']}"
                    ),
                )
                print(
                    "Queue idle. "
                    f"processed={result.processed} succeeded={result.succeeded} "
                    f"failed={result.failed} cancelled={result.cancelled}"
                )
                raise SystemExit(0 if result.failed == 0 else 1)

        if args.command == "workflow" and args.workflow_command == "list":
            project = resolve_project(args.project)
            raise SystemExit(_print_workflow_list(project, args.json))

        if args.command == "workflow" and args.workflow_command == "show":
            project = resolve_project(args.project)
            manifest = WorkflowStore.open(project, args.run_id).read()
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
            raise SystemExit(0)

        if args.command == "workflow" and args.workflow_command == "timings":
            project = resolve_project(args.project)
            path = WorkflowStore.open(project, args.run_id).paths.timings
            if not path.is_file():
                raise FileNotFoundError(f"Workflow timings not found: {path}")
            value = json.loads(path.read_text(encoding="utf-8"))
            raise SystemExit(_print_timings(value, args.json))

        if args.command == "workflow" and args.workflow_command == "explain":
            project = resolve_project(args.project)
            base_config = _config_with_timeout(
                ApiConfig.from_environment(project), args.api_timeout
            )
            explain_config = replace(
                base_config,
                model=args.model or base_config.model,
                reasoning_effort=args.effort,
            )
            explanation = generate_workflow_explanation(
                project=project,
                run_id=args.run_id,
                config=explain_config,
                language=args.language,
            )
            if explanation.ok:
                print("EXPLANATION SUCCESS")
                print(f"JSON: {explanation.json_path}")
                print(f"Markdown: {explanation.markdown_path}")
                raise SystemExit(0)
            print(
                f"EXPLANATION FAILED (Lean workflow remains succeeded): {explanation.error}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        if args.command == "mathlib-search":
            project = resolve_project(args.project)
            rows = []
            for query in args.query:
                rows.extend(hit.to_dict() for hit in search_mathlib(project, query, args.limit))
            suggestions = suggest_imports(rows)
            if args.json:
                print(
                    json.dumps(
                        {"hits": rows, "import_suggestions": suggestions},
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                for row in rows:
                    print(
                        f"{row['module']}  {row['path']}:{row['line']}\n"
                        f"  {row['snippet']}"
                    )
                if not rows:
                    print("No local Mathlib matches found.")
                if args.suggest_imports and suggestions:
                    print("Precise import candidates (Lean must verify):")
                    for suggestion in suggestions:
                        print(
                            f"  import {suggestion['module']}  "
                            f"queries={suggestion['queries']}"
                        )
            raise SystemExit(0)

        if args.command == "lsp-check":
            project = resolve_project(args.project)
            target = resolve_target(project, args.file)
            values = load_project_config(project)
            values["lsp_mode"] = args.mode or values.get("lsp_mode") or "stdio"
            if args.lsp_command:
                values["lsp_command"] = args.lsp_command
            if args.url:
                values["lsp_url"] = args.url
            if args.startup_timeout is not None:
                values["lsp_startup_timeout_seconds"] = args.startup_timeout
            if args.call_timeout is not None:
                values["lsp_call_timeout_seconds"] = args.call_timeout
            if args.remote_search is not None:
                values["lsp_remote_search"] = args.remote_search
            if args.total_timeout < 1:
                raise ValueError("LSP total timeout must be positive")
            settings = LspSettings.from_values(values)
            collector = LspEvidenceCollector(project=project, settings=settings)
            try:
                evidence = collector.collect(
                    file_path=target,
                    source=target.read_text(encoding="utf-8"),
                    diagnostics="",
                    search_terms=list(args.query),
                    allow_remote_search=settings.remote_search,
                    total_timeout_seconds=args.total_timeout,
                )
                print(json.dumps(evidence, ensure_ascii=False, indent=2))
                raise SystemExit(
                    0 if evidence.get("session", {}).get("status") == "ready" else 1
                )
            finally:
                collector.close()

        if args.command == "workflow" and args.workflow_command == "resume":
            project = resolve_project(args.project)
            store = WorkflowStore.open(project, args.run_id)
            manifest = store.read()
            settings = dict(manifest.get("settings") or {})
            target = resolve_target(project, str(manifest["target_file"]))
            max_attempts = int(
                args.max_attempts
                if args.max_attempts is not None
                else settings.get("max_attempts_total", settings.get("max_attempts", 3))
            )
            max_attempts_per_step = int(
                args.max_attempts_per_step
                if args.max_attempts_per_step is not None
                else settings.get("max_attempts_per_step", max_attempts)
            )
            if max_attempts < 1 or max_attempts_per_step < 1:
                raise ConfigError("Resume candidate budgets must be positive")
            backend_id = str(
                args.agent_backend or settings.get("agent_backend") or "direct"
            )
            saved_models = dict(settings.get("models") or {})
            saved_efforts = dict(settings.get("reasoning_effort") or {})
            common_model = args.model or ""
            initial_model = common_model or str(saved_models.get("plan") or "")
            base_config = _config_with_timeout(
                ApiConfig.for_backend(
                    project,
                    backend_id,
                    model=initial_model,
                ),
                args.api_timeout,
            )
            if args.api_retries is not None:
                if args.api_retries < 0:
                    raise ConfigError("--api-retries cannot be negative")
                base_config = replace(base_config, api_timeout_retries=args.api_retries)
            plan_config = replace(
                base_config,
                model=common_model or str(saved_models.get("plan") or base_config.model),
                reasoning_effort=args.plan_effort or saved_efforts.get("plan") or "high",
            )
            prove_config = replace(
                base_config,
                model=common_model or str(saved_models.get("prove") or base_config.model),
                reasoning_effort=args.prove_effort or saved_efforts.get("prove") or "high",
            )
            review_config = replace(
                base_config,
                model=(
                    args.review_model
                    or common_model
                    or str(saved_models.get("review") or base_config.model)
                ),
                reasoning_effort=args.review_effort or saved_efforts.get("review") or "medium",
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task=str(manifest["task"]),
                plan_config=plan_config,
                prove_config=prove_config,
                review_config=review_config,
                max_attempts=max_attempts,
                max_attempts_per_step=max_attempts_per_step,
                lean_timeout_seconds=int(
                    args.lean_timeout
                    if args.lean_timeout is not None
                    else settings.get("lean_timeout_seconds", 120)
                ),
                lake_executable=args.lake or str(settings.get("lake_executable") or "lake"),
                keep_failed=bool(settings.get("keep_failed", False)),
                formalize_goal=bool(settings.get("formalize_goal", True)),
                import_policy=(
                    args.import_policy
                    or str(settings.get("import_policy") or "auto")
                ),
                planning_mode=(
                    args.planning_mode
                    or str(settings.get("planning_mode") or "planner")
                ),
                protect_existing_statements=bool(
                    settings.get("protect_existing_statements", True)
                ),
                protected_declarations=list(settings.get("protected_declarations") or []),
                resume_run_id=args.run_id,
                agent_backend_id=backend_id,
            )
            print(f"{'SUCCESS' if result.ok else 'FAILED'} after {result.attempts} total candidate(s).")
            print(f"Run ID: {result.run_id}")
            print(f"State: {result.state_dir}")
            raise SystemExit(0 if result.ok else 1)

        if args.command == "workflow" and args.workflow_command == "run":
            project = resolve_project(args.project)
            target = resolve_target(project, args.file)
            if args.max_attempts < 1:
                raise ConfigError("--max-attempts must be positive")
            if args.max_attempts_per_step < 1:
                raise ConfigError("--max-attempts-per-step must be positive")
            if args.agent_backend != "direct" and args.explain:
                raise ConfigError(
                    "Subscription workflow explanation requires a separate explicit direct API call"
                )
            base_config = _config_with_timeout(
                ApiConfig.for_backend(
                    project,
                    args.agent_backend,
                    model=args.model,
                ),
                args.api_timeout,
            )
            if args.api_retries is not None:
                if args.api_retries < 0:
                    raise ConfigError("--api-retries cannot be negative")
                base_config = replace(
                    base_config, api_timeout_retries=args.api_retries
                )
            plan_config = replace(base_config, reasoning_effort=args.plan_effort)
            prove_config = replace(base_config, reasoning_effort=args.prove_effort)
            review_config = replace(
                base_config,
                model=args.review_model or base_config.model,
                reasoning_effort=args.review_effort,
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task=args.task,
                plan_config=plan_config,
                prove_config=prove_config,
                review_config=review_config,
                max_attempts=args.max_attempts,
                max_attempts_per_step=args.max_attempts_per_step,
                lean_timeout_seconds=args.lean_timeout,
                lake_executable=args.lake,
                keep_failed=args.keep_failed,
                formalize_goal=args.formalize_goal,
                import_policy=args.import_policy,
                planning_mode=args.planning_mode,
                protected_declarations=list(args.protect_declaration),
                agent_backend_id=args.agent_backend,
            )
            label = "SUCCESS" if result.ok else "FAILED"
            print(f"{label} after {result.attempts} attempt(s).")
            print(f"Run ID: {result.run_id}")
            print(f"State: {result.state_dir}")
            timing_path = result.state_dir / "timings.json"
            if timing_path.is_file():
                timings = json.loads(timing_path.read_text(encoding="utf-8"))
                print(f"Total time: {float(timings.get('total_seconds', 0)):.3f}s")
            if not result.ok:
                print("Original file restored." if result.restored else "Last candidate kept.")
                if result.final_check.output:
                    print("Last candidate diagnostics:")
                    print(result.final_check.output)
            elif args.explain:
                explain_config = replace(
                    base_config,
                    model=args.explain_model or base_config.model,
                    reasoning_effort=args.explain_effort,
                )
                explanation = generate_workflow_explanation(
                    project=project,
                    run_id=result.run_id,
                    config=explain_config,
                    language=args.explain_language,
                )
                if explanation.ok:
                    print(f"Explanation: {explanation.markdown_path}")
                else:
                    print(
                        "WARNING: Natural-language explanation failed, but the Lean "
                        f"workflow remains succeeded: {explanation.error}",
                        file=sys.stderr,
                    )
            raise SystemExit(0 if result.ok else 1)

        project = resolve_project(args.project)
        target = resolve_target(project, args.file)

        if args.command == "check":
            result = check_lean(project, target, args.timeout, args.lake)
            print(result.output or "Lean check passed with no output.")
            raise SystemExit(0 if result.ok else result.returncode or 1)

        if args.max_attempts < 1:
            raise ConfigError("--max-attempts must be positive")
        config = _config_with_timeout(ApiConfig.from_environment(project), args.api_timeout)
        if args.reasoning_effort is not None:
            config = replace(config, reasoning_effort=args.reasoning_effort)
        result = run_repair_loop(
            project=project,
            target=target,
            task=args.task,
            config=config,
            max_attempts=args.max_attempts,
            lean_timeout_seconds=args.lean_timeout,
            lake_executable=args.lake,
            keep_failed=args.keep_failed,
        )
        if result.ok:
            print(f"SUCCESS after {result.attempts} attempt(s).")
            print(f"Backup: {result.backup_path}")
            raise SystemExit(0)
        print(f"FAILED after {result.attempts} attempt(s).")
        print(f"Backup: {result.backup_path}")
        print("Original file restored." if result.restored else "Last failed edit kept.")
        if result.final_check.output:
            print(result.final_check.output)
        raise SystemExit(1)
    except (
        ProjectError,
        ConfigError,
        ApiError,
        QueueError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
