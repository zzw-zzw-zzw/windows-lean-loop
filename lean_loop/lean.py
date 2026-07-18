from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lean_loop.process_control import ProcessControl, run_controlled_process


class ProjectError(ValueError):
    pass


@dataclass(frozen=True)
class LeanCheck:
    ok: bool
    returncode: int
    output: str
    command: tuple[str, ...]


def resolve_project(project: Path) -> Path:
    resolved = project.expanduser().resolve()
    if not resolved.is_dir():
        raise ProjectError(f"Project directory not found: {resolved}")
    if not (resolved / "lean-toolchain").is_file():
        raise ProjectError(f"Missing lean-toolchain in {resolved}")
    if not any((resolved / name).is_file() for name in ("lakefile.toml", "lakefile.lean")):
        raise ProjectError(f"Missing lakefile.toml or lakefile.lean in {resolved}")
    return resolved


def resolve_target(project: Path, file_name: str) -> Path:
    target = (project / file_name).resolve()
    try:
        target.relative_to(project)
    except ValueError as exc:
        raise ProjectError("Target file must stay inside the Lean project") from exc
    if target.suffix.lower() != ".lean":
        raise ProjectError("Target file must have a .lean extension")
    if not target.is_file():
        raise ProjectError(f"Lean file not found: {target}")
    return target


def check_lean(
    project: Path,
    target: Path,
    timeout_seconds: int,
    lake_executable: str = "lake",
    *,
    process_control: ProcessControl | None = None,
) -> LeanCheck:
    relative = target.relative_to(project)
    command = (lake_executable, "env", "lean", str(relative))
    try:
        completed = run_controlled_process(
            command,
            cwd=project,
            timeout_seconds=timeout_seconds,
            kind="lean",
            control=process_control,
        )
    except FileNotFoundError:
        return LeanCheck(False, 127, f"lake was not found: {lake_executable}", command)
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(part for part in (exc.stdout, exc.stderr) if part)
        return LeanCheck(False, 124, f"Lean check timed out.\n{output}", command)

    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    return LeanCheck(completed.returncode == 0, completed.returncode, output, command)


def find_program(name: str) -> str | None:
    return shutil.which(name)
