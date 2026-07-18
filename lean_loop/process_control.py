from __future__ import annotations

import os
import signal
import subprocess
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence


class ProcessCancelled(RuntimeError):
    pass


class ProcessControl(Protocol):
    def cancel_requested(self) -> bool: ...

    def process_started(self, pid: int, kind: str) -> None: ...

    def process_finished(self, pid: int) -> None: ...


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_controlled_process(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout_seconds: int,
    kind: str,
    control: ProcessControl | None = None,
    stdout_line_callback: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    if control is not None:
        try:
            control.process_started(process.pid, kind)
        except Exception:
            terminate_process_tree(process)
            raise

    if stdout_line_callback is not None:
        return _collect_streaming_process(
            process,
            command=command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            kind=kind,
            control=control,
            stdout_line_callback=stdout_line_callback,
        )

    started = time.monotonic()
    pending_input = input_text
    stdout = ""
    stderr = ""
    try:
        while True:
            if control is not None and control.cancel_requested():
                terminate_process_tree(process)
                raise ProcessCancelled(f"Cancelled while running {kind} (PID {process.pid})")
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                terminate_process_tree(process)
                raise subprocess.TimeoutExpired(command, timeout_seconds, stdout, stderr)
            try:
                stdout, stderr = process.communicate(
                    input=pending_input,
                    timeout=min(0.2, remaining),
                )
                break
            except subprocess.TimeoutExpired as exc:
                pending_input = None
                if isinstance(exc.stdout, str):
                    stdout = exc.stdout
                if isinstance(exc.stderr, str):
                    stderr = exc.stderr
    except KeyboardInterrupt:
        terminate_process_tree(process)
        raise
    finally:
        if control is not None:
            control.process_finished(process.pid)

    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _collect_streaming_process(
    process: subprocess.Popen[str],
    *,
    command: Sequence[str],
    input_text: str | None,
    timeout_seconds: int,
    kind: str,
    control: ProcessControl | None,
    stdout_line_callback: Callable[[str], None],
) -> subprocess.CompletedProcess[str]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Streaming process requires stdout and stderr pipes")
    if input_text is not None:
        if process.stdin is None:
            raise RuntimeError("Streaming process requires an stdin pipe")
        process.stdin.write(input_text)
        process.stdin.close()

    lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def drain(name: str, pipe: object) -> None:
        reader = getattr(pipe, "readline")
        try:
            while True:
                line = reader()
                if line == "":
                    break
                lines.put((name, line))
        finally:
            lines.put((name, None))

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    completed_streams: set[str] = set()
    try:
        while process.poll() is None or len(completed_streams) < 2:
            if control is not None and control.cancel_requested():
                terminate_process_tree(process)
                raise ProcessCancelled(f"Cancelled while running {kind} (PID {process.pid})")
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds:
                terminate_process_tree(process)
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    "".join(stdout_parts),
                    "".join(stderr_parts),
                )
            try:
                name, line = lines.get(timeout=min(0.2, timeout_seconds - elapsed))
            except queue.Empty:
                continue
            if line is None:
                completed_streams.add(name)
            elif name == "stdout":
                stdout_parts.append(line)
                stdout_line_callback(line)
            else:
                stderr_parts.append(line)
        process.wait()
    except KeyboardInterrupt:
        terminate_process_tree(process)
        raise
    finally:
        if process.poll() is None:
            terminate_process_tree(process)
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
        if control is not None:
            control.process_finished(process.pid)

    return subprocess.CompletedProcess(
        list(command), process.returncode, "".join(stdout_parts), "".join(stderr_parts)
    )
